"""Column presets. Each template maps LeadScout columns to the header names a
cold-email tool expects on import, in order. Columns not listed are dropped."""

from __future__ import annotations

DEFAULT_COLUMNS = [
    "business_name", "website", "email", "contact_name", "designation", "decision_maker", "owner_name", "owner_title",
    "other_contacts", "key_people",
    "email_status", "email_last_seen", "email_found_in", "business_activity", "activity_reason",
    "email_type", "first_name", "phone",
    "facebook", "instagram", "linkedin", "x_twitter", "youtube", "tiktok",
    "industry", "city", "region", "country", "score", "platform", "source",
]
SOCIAL_COLUMNS = [("facebook", "facebook"), ("instagram", "instagram"), ("x_twitter", "twitter"),
                  ("youtube", "youtube"), ("tiktok", "tiktok")]

TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "default": [(c, c) for c in DEFAULT_COLUMNS],
    "instantly": [
        ("email", "email"), ("first_name", "first_name"), ("business_name", "company_name"),
        ("website", "website"), ("phone", "phone"), ("city", "city"), ("region", "state"),
        ("country", "country"), ("industry", "industry"), ("linkedin", "linkedin"),
        ("score", "lead_score"), ("designation", "job_title"), ("other_contacts", "other_contacts"),
        *SOCIAL_COLUMNS,
    ],
    "smartlead": [
        ("email", "email"), ("first_name", "first_name"), ("business_name", "company_name"),
        ("designation", "job_title"), ("website", "website"), ("phone", "phone_number"), ("city", "location"),
        ("linkedin", "linkedin_profile"), ("industry", "industry"), ("score", "lead_score"),
        *SOCIAL_COLUMNS,
    ],
    "lemlist": [
        ("email", "email"), ("first_name", "firstName"), ("designation", "jobTitle"), ("business_name", "companyName"),
        ("domain", "companyDomain"), ("website", "website"), ("phone", "phone"), ("linkedin", "linkedinUrl"),
        ("city", "city"), ("industry", "industry"), ("score", "leadScore"),
        *SOCIAL_COLUMNS,
    ],
    "hubspot": [
        ("email", "Email"), ("first_name", "First Name"), ("designation", "Job Title"), ("business_name", "Company Name"),
        ("website", "Website URL"), ("phone", "Phone Number"), ("city", "City"),
        ("region", "State/Region"), ("country", "Country/Region"), ("industry", "Industry"),
        ("linkedin", "LinkedIn Company Page"), ("facebook", "Facebook Company Page"),
        ("x_twitter", "Twitter Handle"), ("instagram", "Instagram"), ("youtube", "YouTube"), ("tiktok", "TikTok"),
    ],
}
