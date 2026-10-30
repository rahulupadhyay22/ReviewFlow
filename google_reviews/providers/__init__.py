"""The single V1 GoogleSyncProvider. services.py calls get_provider() so a test
can replace the whole provider; the HTTP boundary (urllib.request.urlopen as
seen by google_business_profile) is the normal thing to mock."""
from google_reviews.providers.google_business_profile import GoogleBusinessProfileProvider


def get_provider():
    return GoogleBusinessProfileProvider()
