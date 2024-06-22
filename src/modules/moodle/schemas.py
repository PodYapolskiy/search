from typing import Literal

from pydantic import Field

from src.custom_pydantic import CustomModel
from src.storages.mongo.moodle import MoodleCourseSchema, MoodleContentSchema


class InCourse(MoodleCourseSchema):
    course_id: int = Field(..., validation_alias="id")


class InCourses(CustomModel):
    courses: list[InCourse]


class InModule(CustomModel):
    module_id: int = Field(..., validation_alias="id")
    module_name: str = Field(..., validation_alias="name")
    module_modname: str = Field(..., validation_alias="modname")
    contents: list[MoodleContentSchema] = []


class InSection(CustomModel):
    section_id: int = Field(..., validation_alias="id")
    section_summary: str = Field(..., validation_alias="summary")
    modules: list[InModule]


class InSections(CustomModel):
    course_id: int
    course_fullname: str
    sections: list[InSection]


class FileOnlyContentRef(CustomModel):
    course_id: int
    module_id: int
    type: Literal["file"]
    filename: str

    def to_object(self):
        return f"moodle/{self.course_id}/{self.module_id}/{self.filename}"


class UploadableContentRef(FileOnlyContentRef):
    timecreated: int | None = None
    timemodified: int | None = None
