from __future__ import annotations

DISCOVERY_TEMPLATES = [
    {"name": "submit-music-country", "template": '"{country}" radio station "submit music"', "locale": "en"},
    {"name": "new-artist-airplay", "template": '"{country}" "radio" "new artist submission"', "locale": "en"},
    {"name": "playlist-submission", "template": '"{country}" "radio" "playlist submission"', "locale": "en"},
    {"name": "genre-radio", "template": '"{country}" "{genre}" "radio station"', "locale": "en"},
    {"name": "music-director", "template": '"{country}" "radio" "music director" "submit"', "locale": "en"},
    {"name": "show-host-contact", "template": '"{country}" "radio show" host contact music submission', "locale": "en"},
    {"name": "presenter-playlist", "template": '"{country}" "radio presenter" playlist submit track', "locale": "en"},
]

DEFAULT_COUNTRIES = ["US", "GB", "DE", "FR", "ES", "IT", "NL", "SE", "NO", "CA", "AU", "BR", "ZA", "IN", "JP"]
DEFAULT_GENRES = ["rock", "pop", "hip hop", "jazz", "electronic", "country", "latin", "reggae"]


def generate_queries() -> list[dict]:
    queries: list[dict] = []
    for country in DEFAULT_COUNTRIES:
        for tpl in DISCOVERY_TEMPLATES:
            if "{genre}" in tpl["template"]:
                for genre in DEFAULT_GENRES:
                    queries.append(
                        {
                            "query": tpl["template"].format(country=country, genre=genre),
                            "template_name": tpl["name"],
                            "country": country,
                            "locale": tpl["locale"],
                        }
                    )
            else:
                queries.append(
                    {
                        "query": tpl["template"].format(country=country),
                        "template_name": tpl["name"],
                        "country": country,
                        "locale": tpl["locale"],
                    }
                )
    return queries


def generate_queries_for_country(country: str) -> list[dict]:
    queries: list[dict] = []
    c = (country or "").strip().upper()
    if not c:
        return queries

    for tpl in DISCOVERY_TEMPLATES:
        if "{genre}" in tpl["template"]:
            for genre in DEFAULT_GENRES:
                queries.append(
                    {
                        "query": tpl["template"].format(country=c, genre=genre),
                        "template_name": tpl["name"],
                        "country": c,
                        "locale": tpl["locale"],
                    }
                )
        else:
            queries.append(
                {
                    "query": tpl["template"].format(country=c),
                    "template_name": tpl["name"],
                    "country": c,
                    "locale": tpl["locale"],
                }
            )
    return queries
