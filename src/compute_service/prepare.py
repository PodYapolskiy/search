import logging
import time
from multiprocessing import Pool
from pathlib import Path

import httpx
import pymupdf
import pymupdf4llm
from pymupdf4llm import join_chunks
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from torch import cuda

from src.compute_service.bm25 import Bm25
from src.compute_service.cache import PDF_TO_TEXT_VERSION
from src.compute_service.chunker import CustomTokenTextSplitter
from src.compute_service.text import clean_text
from src.config import settings
from src.modules.compute.schemas import Corpora
from src.modules.minio.schemas import MoodleFileObject
from src.modules.moodle.utils import content_to_minio_object
from src.storages.minio import minio_client

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("compute.prepare")

_1 = time.monotonic()
device = "cuda" if cuda.is_available() else "cpu"
bi_encoder = SentenceTransformer(settings.compute_settings.bi_encoder_name, trust_remote_code=True, device=device)
bi_encoder.share_memory()
chunk_splitter = CustomTokenTextSplitter(
    tokenizer=bi_encoder.tokenizer, chunk_size=bi_encoder.max_seq_length, chunk_overlap=25
)
bm25 = Bm25()
qdrant = QdrantClient(settings.compute_settings.qdrant_url.get_secret_value())
QDRANT_COLLECTION = settings.compute_settings.qdrant_collection_name

if not qdrant.collection_exists(QDRANT_COLLECTION):
    qdrant.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config={
            "dense": models.VectorParams(
                size=bi_encoder.get_sentence_embedding_dimension(), distance=models.Distance.DOT
            )
        },
        sparse_vectors_config={
            "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
    )
    # create indexes
    qdrant.create_payload_index(
        QDRANT_COLLECTION, "document-ref.course_id", field_schema=models.PayloadSchemaType.INTEGER
    )
    qdrant.create_payload_index(
        QDRANT_COLLECTION, "document-ref.module_id", field_schema=models.PayloadSchemaType.INTEGER
    )
    qdrant.create_payload_index(
        QDRANT_COLLECTION, "document-ref.filename", field_schema=models.PayloadSchemaType.KEYWORD
    )

_2 = time.monotonic()

logger.info(f"Initialized in {_2 - _1:.2f} seconds")


def get_client() -> httpx.Client:
    return httpx.Client(
        base_url=f"{settings.compute_settings.api_url}/compute",
        headers={"Authorization": f"Bearer {settings.compute_settings.auth_token}"},
    )


def fetch_corpora() -> Corpora:
    with get_client() as session:
        response = session.get("/corpora")
        response.raise_for_status()
        corpora_data = response.json()
        corpora = Corpora.model_validate(corpora_data)
    return corpora


def object_pipeline(obj: MoodleFileObject):
    s3_object_name = content_to_minio_object(obj.course_id, obj.module_id, obj.filename)

    file_path = Path("s3") / s3_object_name
    if not file_path.exists():
        minio_client.fget_object(settings.minio.bucket, s3_object_name, str(file_path))

    text_path = Path("cache") / f"text-{PDF_TO_TEXT_VERSION}" / s3_object_name
    if not text_path.exists():
        _1 = time.monotonic()

        text_path.parent.mkdir(parents=True, exist_ok=True)
        with open(text_path, "w") as f:
            doc = pymupdf.Document(file_path)
            output = join_chunks(pymupdf4llm.process_document(doc, graphics_limit=1000))
            doc.close()
            f.write(output)
        _2 = time.monotonic()
        logger.info(f"Converted {obj.filename} to text in {_2 - _1:.2f} seconds")
    else:
        with open(text_path) as f:
            output = f.read()
    chunks = []

    meta_prefix = obj.meta_prefix()

    page_text = clean_text(meta_prefix + output)
    splitted = chunk_splitter.split_text(page_text)
    for j, text in enumerate(splitted):
        chunks.append(
            {
                "text": text,
                "document-ref": {
                    "course_id": obj.course_id,
                    "module_id": obj.module_id,
                    "filename": obj.filename,
                },
                "chunk-ref": {
                    "chunk_number": j,
                },
            }
        )
    return chunks


def save_file_chunks_to_qdrant(chunks: list[dict]):
    if not chunks:
        return

    ref = chunks[0]["document-ref"]
    must = [
        models.FieldCondition(key=f"document-ref.{key}", match=models.MatchValue(value=value))
        for key, value in ref.items()
    ]
    duplicates_filter = models.Filter(must=must)

    exact_match = qdrant.count(QDRANT_COLLECTION, count_filter=duplicates_filter).count
    if exact_match == len(chunks):
        logger.info(f"Skipping {len(chunks)} chunks as they are already in Qdrant")
        return

    qdrant.delete(QDRANT_COLLECTION, models.FilterSelector(filter=duplicates_filter), wait=True)
    texts = [chunk["text"] for chunk in chunks]
    _1 = time.monotonic()
    dense_vectors = bi_encoder.encode(
        texts,
        show_progress_bar=False,
        batch_size=settings.compute_settings.bi_encoder_batch_size,
    )
    _2 = time.monotonic()
    logger.info(f"Encoded [Dense] {len(chunks)} chunks in {_2 - _1:.2f} seconds")

    _1 = time.monotonic()
    sparse_vectors = list(bm25.embed(texts))
    _2 = time.monotonic()
    logger.info(f"Encoded [Sparse] {len(chunks)} chunks in {_2 - _1:.2f} seconds")

    vectors = []
    for dense_vector, sparse_vector in zip(dense_vectors, sparse_vectors):
        vectors.append({"dense": dense_vector, "bm25": sparse_vector})

    qdrant.upload_collection(QDRANT_COLLECTION, payload=chunks, vectors=vectors)
    logger.info(f"Saved +{len(chunks)} chunks to Qdrant")


def corpora_to_qdrant(corpora: Corpora):
    logger.info(f"Processing {len(corpora.moodle_files)} items")
    collection_len = 0

    _1 = time.monotonic()
    items = [item for item in corpora.moodle_files if item.filename.endswith(".pdf")]

    with Pool(processes=settings.compute_settings.num_workers) as pool:
        file_chunks = pool.imap_unordered(object_pipeline, items)
        for chunks in file_chunks:
            if chunks:
                save_file_chunks_to_qdrant(chunks)
                collection_len += len(chunks)
    _2 = time.monotonic()
    logger.info(f"Processed {collection_len} chunks in {_2 - _1:.2f} seconds")


def no_corpora_changes(prev_corpora: Corpora | None, corpora: Corpora) -> bool:
    if prev_corpora is None:
        return False
    prev_moodle_files = prev_corpora.moodle_files
    corpora_moodle_files = corpora.moodle_files

    _prev = [_.model_dump() for _ in prev_moodle_files]
    _curr = [_.model_dump() for _ in corpora_moodle_files]
    return _prev == _curr


def main():
    logger.info(f"Fetch corpora from API every {settings.compute_settings.corpora_update_period} seconds")
    prev_corpora = None
    while True:
        corpora = fetch_corpora()

        if no_corpora_changes(prev_corpora, corpora):
            logger.info("No corpora changes")
        else:
            logger.info(f"Populated by {len(corpora.moodle_files)} corpora entries")
            corpora_to_qdrant(corpora)
            prev_corpora = corpora

        # Wait for the specified period before fetching tasks again
        time.sleep(settings.compute_settings.corpora_update_period)


if __name__ == "__main__":
    main()
