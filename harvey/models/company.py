"""Company data model."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Company(BaseModel):
    id: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    description: str = ""
    industry: str = ""
    company_size: str = ""
    location: str = ""
    source: str = ""          # how we found them: google_dork, linkedin, company_scrape
    source_url: str = ""      # specific URL where we found the info
    notes: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        """Domains are dedup keys — normalize case/whitespace."""
        return (v or "").strip().lower()
