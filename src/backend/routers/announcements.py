"""
Announcement endpoints for the High School Management System API
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from ..database import announcements_collection, teachers_collection

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/announcements",
    tags=["announcements"]
)


class AnnouncementPayload(BaseModel):
    """Create/update payload for an announcement."""

    message: str
    expires_at: str
    starts_at: Optional[str] = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("Message is required")
        if len(text) > 400:
            raise ValueError("Message must be 400 characters or less")
        return text


def require_signed_in_teacher(teacher_username: Optional[str]) -> Dict[str, Any]:
    """Require a valid signed-in teacher/admin user."""
    if not teacher_username:
        raise HTTPException(status_code=401, detail="Authentication required")

    teacher = teachers_collection.find_one({"_id": teacher_username})
    if not teacher:
        raise HTTPException(status_code=401, detail="Invalid user session")

    return teacher


def parse_iso_datetime(value: Optional[str], field_name: str, required: bool = False) -> Optional[datetime]:
    """Parse date values from frontend into timezone-aware UTC datetimes."""
    if value in (None, ""):
        if required:
            raise HTTPException(status_code=422, detail=f"{field_name} is required")
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name} format") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def serialize_announcement(record: Dict[str, Any]) -> Dict[str, Any]:
    """Convert MongoDB announcement document to API response shape."""
    def to_iso(value: Optional[datetime]) -> Optional[str]:
        if not value:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    return {
        "id": str(record["_id"]),
        "message": record["message"],
        "starts_at": to_iso(record.get("starts_at")),
        "expires_at": to_iso(record.get("expires_at")),
        "created_by": record.get("created_by"),
        "created_at": to_iso(record.get("created_at")),
        "updated_at": to_iso(record.get("updated_at")),
    }


@router.get("", response_model=List[Dict[str, Any]])
def get_active_announcements() -> List[Dict[str, Any]]:
    """Return active announcements for all visitors."""
    now = datetime.now(timezone.utc)
    query = {
        "expires_at": {"$gte": now},
        "$or": [
            {"starts_at": None},
            {"starts_at": {"$exists": False}},
            {"starts_at": {"$lte": now}}
        ]
    }

    try:
        announcements = announcements_collection.find(query).sort("expires_at", 1)
        return [serialize_announcement(item) for item in announcements]
    except Exception:
        logger.exception("Failed to fetch active announcements")
        raise HTTPException(status_code=500, detail="Unable to load announcements at this time")


@router.get("/manage", response_model=List[Dict[str, Any]])
def get_all_announcements_for_management(teacher_username: Optional[str] = Query(None)) -> List[Dict[str, Any]]:
    """Return all announcements for authenticated users managing content."""
    require_signed_in_teacher(teacher_username)

    try:
        announcements = announcements_collection.find({}).sort("expires_at", 1)
        return [serialize_announcement(item) for item in announcements]
    except Exception:
        logger.exception("Failed to fetch announcement management list")
        raise HTTPException(status_code=500, detail="Unable to load announcements at this time")


@router.post("", response_model=Dict[str, Any])
def create_announcement(payload: AnnouncementPayload, teacher_username: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Create a new announcement. Requires signed-in user."""
    teacher = require_signed_in_teacher(teacher_username)

    starts_at = parse_iso_datetime(payload.starts_at, "starts_at")
    expires_at = parse_iso_datetime(payload.expires_at, "expires_at", required=True)

    if starts_at and expires_at <= starts_at:
        raise HTTPException(status_code=422, detail="Expiration must be after the start date")

    now = datetime.now(timezone.utc)
    new_announcement = {
        "message": payload.message,
        "starts_at": starts_at,
        "expires_at": expires_at,
        "created_by": teacher["username"],
        "created_at": now,
        "updated_at": now,
    }

    try:
        result = announcements_collection.insert_one(new_announcement)
        created = announcements_collection.find_one({"_id": result.inserted_id})
        return serialize_announcement(created)
    except Exception:
        logger.exception("Failed to create announcement")
        raise HTTPException(status_code=500, detail="Unable to save announcement")


@router.put("/{announcement_id}", response_model=Dict[str, Any])
def update_announcement(
    announcement_id: str,
    payload: AnnouncementPayload,
    teacher_username: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Update an existing announcement. Requires signed-in user."""
    require_signed_in_teacher(teacher_username)

    starts_at = parse_iso_datetime(payload.starts_at, "starts_at")
    expires_at = parse_iso_datetime(payload.expires_at, "expires_at", required=True)

    if starts_at and expires_at <= starts_at:
        raise HTTPException(status_code=422, detail="Expiration must be after the start date")

    try:
        announcement_object_id = ObjectId(announcement_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Announcement not found") from exc

    updates = {
        "message": payload.message,
        "starts_at": starts_at,
        "expires_at": expires_at,
        "updated_at": datetime.now(timezone.utc),
    }

    try:
        result = announcements_collection.update_one({"_id": announcement_object_id}, {"$set": updates})
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Announcement not found")

        updated = announcements_collection.find_one({"_id": announcement_object_id})
        return serialize_announcement(updated)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update announcement")
        raise HTTPException(status_code=500, detail="Unable to update announcement")


@router.delete("/{announcement_id}", response_model=Dict[str, str])
def delete_announcement(announcement_id: str, teacher_username: Optional[str] = Query(None)) -> Dict[str, str]:
    """Delete an announcement. Requires signed-in user."""
    require_signed_in_teacher(teacher_username)

    try:
        announcement_object_id = ObjectId(announcement_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Announcement not found") from exc

    try:
        result = announcements_collection.delete_one({"_id": announcement_object_id})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Announcement not found")

        return {"message": "Announcement deleted"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to delete announcement")
        raise HTTPException(status_code=500, detail="Unable to delete announcement")
