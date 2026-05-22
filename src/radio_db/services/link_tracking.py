"""
Link tracking and UTM parameter utilities for outreach campaigns.

This module provides tools to track links in outreach emails, including:
- URL rewriting with tracking codes
- UTM parameter generation
- Short URL creation with tracking
- Analytics report generation
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import hashlib
import secrets
from uuid import uuid4
import os

from radio_db.db import SessionLocal
from radio_db.models.entities import LinkTrackingCode
from radio_db.config import settings


@dataclass
class TrackingCode:
    """Represents a tracking code for link analytics."""
    
    code: str
    campaign_id: int
    recipient_email: str
    link_type: str  # "submission_form", "website", "contact_email"
    original_url: str
    created_at: datetime
    
    @classmethod
    def generate(
        cls,
        campaign_id: int,
        recipient_email: str,
        link_type: str,
        original_url: str,
    ) -> TrackingCode:
        """Generate a new tracking code."""
        # Create deterministic code from campaign + email + link_type + timestamp
        seed = f"{campaign_id}:{recipient_email}:{link_type}:{datetime.utcnow().isoformat()}"
        code = hashlib.sha256(seed.encode()).hexdigest()[:12]
        
        return cls(
            code=code,
            campaign_id=campaign_id,
            recipient_email=recipient_email,
            link_type=link_type,
            original_url=original_url,
            created_at=datetime.utcnow(),
        )


class LinkTracker:
    """Utility for tracking links in outreach campaigns."""
    
    @staticmethod
    def add_utm_parameters(
        url: str,
        campaign_name: str,
        source: str = "email",
        medium: str = "outreach",
        content: Optional[str] = None,
    ) -> str:
        """
        Add UTM tracking parameters to a URL.
        
        Args:
            url: Original URL
            campaign_name: Campaign name
            source: UTM source (default: "email")
            medium: UTM medium (default: "outreach")
            content: Optional UTM content for A/B testing
            
        Returns:
            URL with UTM parameters
            
        Example:
            >>> tracker = LinkTracker()
            >>> url = tracker.add_utm_parameters(
            ...     "https://docs.google.com/forms/d/123",
            ...     campaign_name="newcomer_outreach_2026"
            ... )
            >>> # Result: https://docs.google.com/forms/d/123?utm_source=email&utm_medium=outreach&utm_campaign=newcomer_outreach_2026
        """
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        
        # UTM parameters (lowercase for consistency)
        query_params["utm_source"] = [source]
        query_params["utm_medium"] = [medium]
        query_params["utm_campaign"] = [campaign_name]
        
        if content:
            query_params["utm_content"] = [content]
        
        # Flatten lists for urlencode
        flat_params = {k: v[0] if v else "" for k, v in query_params.items()}
        new_query = urlencode(flat_params)
        
        new_parsed = parsed._replace(query=new_query)
        return urlunparse(new_parsed)
    
    @staticmethod
    def add_tracking_code(
        url: str,
        tracking_code: str,
        param_name: str = "radio_db_tracking",
    ) -> str:
        """
        Add a tracking code parameter to a URL.
        
        Args:
            url: Original URL
            tracking_code: Tracking code to add
            param_name: Parameter name (default: "radio_db_tracking")
            
        Returns:
            URL with tracking code parameter
        """
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        
        query_params[param_name] = [tracking_code]
        
        flat_params = {k: v[0] if v else "" for k, v in query_params.items()}
        new_query = urlencode(flat_params)
        
        new_parsed = parsed._replace(query=new_query)
        return urlunparse(new_parsed)
    
    @staticmethod
    def wrap_link(
        url: str,
        campaign_id: int,
        recipient_email: str,
        link_type: str,
        campaign_name: str,
        utm_content: Optional[str] = None,
    ) -> tuple[str, TrackingCode]:
        """
        Wrap a link with full tracking: UTM parameters + tracking code.
        
        Args:
            url: Original URL to track
            campaign_id: Campaign ID for tracking
            recipient_email: Email address of recipient
            link_type: Type of link ("submission_form", "website", "contact_email")
            campaign_name: Campaign name for UTM
            utm_content: Optional UTM content for A/B testing
            
        Returns:
            Tuple of (tracked_url, tracking_code)
            
        Example:
            >>> tracker = LinkTracker()
            >>> tracked_url, code = tracker.wrap_link(
            ...     url="https://docs.google.com/forms/d/123",
            ...     campaign_id=42,
            ...     recipient_email="artist@example.com",
            ...     link_type="submission_form",
            ...     campaign_name="newcomers_2026",
            ... )
            >>> print(f"Track this: {code.code}")
        """
        # Generate tracking code dataclass
        tracking_code = TrackingCode.generate(
            campaign_id=campaign_id,
            recipient_email=recipient_email,
            link_type=link_type,
            original_url=url,
        )

        # Add UTM parameters
        url_with_utm = LinkTracker.add_utm_parameters(
            url=url,
            campaign_name=campaign_name,
            content=utm_content,
        )

        # Add tracking code parameter to the destination URL (so destination sees utm+code)
        final_destination = LinkTracker.add_tracking_code(
            url=url_with_utm,
            tracking_code=tracking_code.code,
        )

        # Persist tracking code record and return a redirect URL pointing to our app
        db = SessionLocal()
        try:
            record = LinkTrackingCode(
                code=tracking_code.code,
                campaign_id=campaign_id,
                recipient_email=recipient_email,
                link_type=link_type,
                original_url=final_destination,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
        finally:
            db.close()

        # Build redirect URL. Prefer configured contact_execute_website as base, otherwise relative path.
        base = settings.contact_execute_website.rstrip("/") if settings.contact_execute_website else ""
        if base:
            redirect_url = f"{base}/r/{record.code}"
        else:
            redirect_url = f"/r/{record.code}"

        return redirect_url, tracking_code


class EmailTracker:
    """Utility for generating trackable email templates."""
    
    @staticmethod
    def wrap_email_content(
        email_body: str,
        campaign_id: int,
        recipient_email: str,
        campaign_name: str,
        tracking_pixel_url: Optional[str] = None,
    ) -> str:
        """
        Wrap email content with tracking pixel (if provided).
        
        Args:
            email_body: Original email body
            campaign_id: Campaign ID
            recipient_email: Recipient email
            campaign_name: Campaign name
            tracking_pixel_url: Optional tracking pixel URL
            
        Returns:
            Email body with tracking pixel appended
        """
        if not tracking_pixel_url:
            return email_body
        
        # Create tracking pixel with parameters
        tracking_params = {
            "campaign_id": str(campaign_id),
            "recipient": recipient_email,
            "campaign": campaign_name,
            "opened_at": "{{opened_at}}",  # Placeholder for server-side timestamp
        }
        pixel_url = tracking_pixel_url + "?" + urlencode(tracking_params)
        
        # Append 1x1 invisible tracking pixel
        pixel_html = f'<img src="{pixel_url}" alt="" width="1" height="1" style="display:none;" />'
        
        return email_body + "\n" + pixel_html


# Example usage and integration
if __name__ == "__main__":
    tracker = LinkTracker()
    
    # Test: Track a submission form
    form_url = "https://docs.google.com/forms/d/e/1FAIpQLSegv5YaLlx3ntIfdUvF6Cb_mZCzyD5e4PsYwhnFG9b-DVc8qg/viewform"
    tracked_url, code = tracker.wrap_link(
        url=form_url,
        campaign_id=1,
        recipient_email="test@example.com",
        link_type="submission_form",
        campaign_name="jonas_test_outreach",
        utm_content="form_button",
    )
    
    print("Original URL:")
    print(form_url)
    print("\nTracked URL:")
    print(tracked_url)
    print(f"\nTracking Code: {code.code}")
    print(f"Campaign ID: {code.campaign_id}")
    print(f"Recipient: {code.recipient_email}")
