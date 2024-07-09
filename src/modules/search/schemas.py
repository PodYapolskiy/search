import re
from typing import Literal, Annotated, TypeAlias

from beanie import PydanticObjectId
from pydantic import Discriminator, model_validator

from src.custom_pydantic import CustomModel
from src.storages.mongo import MoodleEntry


class PdfLocation(CustomModel):
    page_index: int
    "Page index in the PDF file. Starts from 1."


class MoodleSource(CustomModel):
    type: Literal["moodle"] = "moodle"
    display_name: str = "-"
    "Display name of the resource."
    breadcrumbs: list[str] = ["Moodle"]
    "Breadcrumbs to the resource."
    course_id: int
    "Course ID in the Moodle system."
    course_name: str
    "Course name in the Moodle system."
    module_id: int
    "Module ID in the Moodle system (resources)."
    module_name: str
    "Module name in the Moodle system."
    resource_type: str
    "Type of the resource."
    filename: str | None = None
    "Filename of the resource."
    link: str
    "Anchor URL to the resource on Moodle."
    resource_preview_url: str
    "URL to get the preview of the resource."
    resource_download_url: str
    "URL to download the resource."
    preview_location: PdfLocation | None = None

    @model_validator(mode="before")
    def set_breadcrumbs_and_display_name(cls, data):
        if "course_name" not in data or "module_name" not in data:
            return data
        course_name = data["course_name"]
        # remove "/ Глубокое обучение для задач поиска" from "[Sum24] Deep Learning for Search / Глубокое обучение
        # для задач поиска"
        course_name = course_name.split(" / ")[0]
        data["breadcrumbs"] = ["Moodle", course_name, data["module_name"]]
        data["display_name"] = data["module_name"]
        return data


class TelegramSource(CustomModel):
    type: Literal["telegram"] = "telegram"
    display_name: str = "-"
    "Display name of the resource."
    breadcrumbs: list[str] = ["Telegram"]
    "Breadcrumbs to the resource."
    chat_username: str
    "Username of the chat, channel, group"
    chat_title: str
    "Title of the chat, channel, group"
    message_id: int
    "Message ID in the chat"
    link: str
    "Link to the message"

    @model_validator(mode="before")
    def set_breadcrumbs(cls, data):
        if "chat_title" not in data:
            return data
        data["breadcrumbs"] = ["Telegram", data["chat_title"]]
        display_name = ""
        text = data.get("text") or data.get("caption")

        if text:
            # get first line of the message
            display_name = text.split("\n")[0]
            # only normal characters
            display_name = re.sub(r"[^a-zA-Z0-9 ]", "", display_name)
        data["display_name"] = display_name or "-"
        return data


Sources: TypeAlias = Annotated[MoodleSource | TelegramSource, Discriminator("type")]


class MoodleEntryWithScore(MoodleEntry):
    score: float
    "Score of the search response. Multiple scores if was an aggregation of multiple chunks."


class SearchResponse(CustomModel):
    source: Sources
    "Relevant source for the search."
    score: float | list[float] | None = None
    "Score of the search response. Multiple scores if was an aggregation of multiple chunks. Optional."


class SearchResponses(CustomModel):
    searched_for: str
    "Text that was searched for."
    responses: list[SearchResponse]
    "Responses to the search query."
    search_query_id: PydanticObjectId | None = None
    "Assigned search query index"
