# Chemin exact : scripts/ingest_outages.py
import inspect
import json
import logging
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Importation sécurisée de requests-cache (dégradation gracieuse si absent)
try:
    import requests_cache
    HAS_REQUESTS_CACHE = True
except ImportError:
    HAS_REQUESTS_CACHE = False

# Configuration du journal d'exécution (logging)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Définition des chemins absolus à partir de la racine du dépôt
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUTAGES_FILE = os.path.join(DATA_DIR, 'outages.json')

# Dossier et fichier dédiés au cache HTTP SQLite (restauré par actions/cache)
CACHE_DIR = os.path.join(BASE_DIR, '.cache')
CACHE_DB_PATH = os.path.join(CACHE_DIR, 'http_cache.sqlite')
CACHE_EXPIRE_SECONDS = 3600  # Validité du cache : 1 heure (3600 secondes)

# Paramètres métier et réseau
MAX_AGE_DAYS = 90  # Rétention des incidents limitée à 90 jours
MAX_UPDATES_PER_INCIDENT = 4  # Maximum 4 étapes chronologiques conservées par incident
MAX_UPDATE_TEXT_CHARS = 160  # Troncature stricte pour éviter toute recopie intégrale
REQUEST_TIMEOUT = (5, 15)  # Timeout explicite : (5s connexion, 15s lecture)
USER_AGENT = "Vecteur1-UptimeMonitor/1.0 (+https://uptime-intel.com; contact@uptime-intel.com; Public Status Aggregator)"


class LoggedRetry(Retry):
    """
    Sous-classe de urllib3.util.Retry qui journalise explicitement en français
    chaque tentative de répétition (retry) avant qu'elle ne soit exécutée.
    """
    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        new_retry = super().increment(
            method=method, url=url, response=response, error=error, _pool=_pool, _stacktrace=_stacktrace
        )
        attempt_num = 5 - (new_retry.total if isinstance(new_retry.total, int) else 0)
        if response is not None:
            retry_after = response.headers.get("Retry-After", "non spécifié")
            logger.warning(
                f"[RETRY #{attempt_num}] Statut HTTP {response.status} reçu sur {url} "
                f"(En-tête Retry-After: {retry_after}). Attente backoff + jitter en cours..."
            )
        elif error is not None:
            logger.warning(
                f"[RETRY #{attempt_num}] Erreur réseau transitoire sur {url} ({error}). "
                f"Attente backoff + jitter en cours..."
            )
        return new_retry


def clean_plain_excerpt(raw_text, max_chars=MAX_UPDATE_TEXT_CHARS):
    """
    Nettoie un texte brut issu d'une API tierce (supprime les balises HTML et les retours
    à la ligne multiples) et le tronque proprement pour ne jamais recopier une source intégralement.
    """
    if not raw_text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(raw_text))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[:max_chars - 3].rstrip() + "..."
    return text


def init_sqlite_wal_mode(db_path):
    """
    Active le mode WAL (Write-Ahead Logging) et un délai d'attente de 30s sur SQLite
    pour prévenir l'erreur 'OperationalError: database is locked'.
    [À VÉRIFIER : comportement de verrouillage SQLite sous Python 3.11/3.12 selon l'OS].
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.Error as e:
        logger.warning(f"Impossible d'activer le mode WAL sur SQLite ({db_path}) : {e}")


def create_resilient_session():
    """
    Crée une session HTTP résiliente combinant cache SQLite persistant et
    politique de répétition conforme RFC 9110 (403 exclu, 429 avec Retry-After, 408/5xx avec jitter).
    """
    if HAS_REQUESTS_CACHE:
        init_sqlite_wal_mode(CACHE_DB_PATH)
        try:
            backend = requests_cache.SQLiteCache(
                db_path=CACHE_DB_PATH,
                wal=True,
                timeout=30.0
            )
            session = requests_cache.CachedSession(
                backend=backend,
                expire_after=CACHE_EXPIRE_SECONDS,
                allowable_methods=('GET', 'HEAD'),
                allowable_codes=(200,),
                ignored_parameters=['api_key', 'token', 'access_token', 'auth', 'key'],
                stale_if_error=False
            )
            logger.info(f"Cache HTTP SQLite activé ({CACHE_DB_PATH}, expiration: {CACHE_EXPIRE_SECONDS}s).")
        except Exception as e:
            logger.warning(f"Échec d'initialisation du cache SQLite ({e}). Repli sur requests.Session standard.")
            session = requests.Session()
    else:
        logger.warning("Bibliothèque requests-cache absente. Exécution sans cache persistant.")
        session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.8"
    })

    retry_kwargs = {
        "total": 4,
        "connect": 3,
        "read": 3,
        "status_forcelist": [408, 429, 500, 502, 503, 504],
        "allowed_methods": frozenset(["HEAD", "GET", "OPTIONS"]),
        "backoff_factor": 1.0,
        "respect_retry_after_header": True,
        "raise_on_status": False
    }

    supported_params = inspect.signature(Retry.__init__).parameters
    if "backoff_jitter" in supported_params:
        retry_kwargs["backoff_jitter"] = 1.0
    if "backoff_max" in supported_params:
        retry_kwargs["backoff_max"] = 60.0

    retry_strategy = LoggedRetry(**retry_kwargs)
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_get(session, url, source_name, params=None):
    """Exécute une requête GET avec timeout explicite et classification RFC 9110."""
    try:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        from_cache = getattr(response, "from_cache", False)

        if response.status_code == 403:
            logger.error(
                f"[{source_name}] Accès refusé (HTTP 403 Forbidden) sur {url}. "
                f"Aucune nouvelle tentative effectuée pour éviter un blocage IP/WAF."
            )
            return None

        if response.status_code == 429:
            logger.error(
                f"[{source_name}] Quota dépassé (HTTP 429 Too Many Requests) persistant sur {url}."
            )
            return None

        response.raise_for_status()
        logger.info(
            f"[{source_name}] Réponse HTTP {response.status_code} reçue "
            f"({'depuis le CACHE SQLite' if from_cache else 'via RÉSEAU direct'})."
        )
        return response

    except requests.exceptions.Timeout as e:
        logger.error(f"[{source_name}] Délai d'attente dépassé (Timeout {REQUEST_TIMEOUT}s) sur {url} : {e}")
    except requests.exceptions.RequestException as e:
        logger.error(f"[{source_name}] Échec HTTP/Réseau définitif sur {url} : {e}")
    return None


def fetch_gcp_incidents(session):
    """Extraction des incidents GCP via le JSON statique public."""
    url = "https://status.cloud.google.com/incidents.json"
    incidents = []
    logger.info("Récupération des incidents GCP...")
    response = safe_get(session, url, "GCP")
    if response is None:
        return None

    try:
        data = response.json()
        for item in data:
            start_date_str = item.get("begin", "")
            if not start_date_str:
                continue

            raw_service = item.get("service_name", "GCP Core Infrastructure")
            if raw_service in ["Multiple Products", "Multiple"]:
                raw_service = "GCP Multi-Service Infrastructure"

            summary = clean_plain_excerpt(item.get("external_desc", ""), 200) or "GCP Service Degradation"
            uri = item.get("uri", "")
            official_url = f"https://status.cloud.google.com/{uri.lstrip('/')}" if uri else "https://status.cloud.google.com/"

            # Extraction des mises à jour GCP (champ 'updates' dans le schéma public GCP)
            updates_list = []
            for upd in item.get("updates", [])[:MAX_UPDATES_PER_INCIDENT]:
                upd_time = upd.get("created") or upd.get("modified") or start_date_str
                upd_status = str(upd.get("status", "update")).lower()
                upd_body = clean_plain_excerpt(upd.get("text", ""), MAX_UPDATE_TEXT_CHARS)
                if upd_body:
                    updates_list.append({
                        "status": upd_status,
                        "timestamp": upd_time,
                        "summary_en": upd_body
                    })

            if not updates_list:
                updates_list.append({
                    "status": "resolved" if item.get("end") else "investigating",
                    "timestamp": start_date_str,
                    "summary_en": clean_plain_excerpt(summary, MAX_UPDATE_TEXT_CHARS)
                })

            incident = {
                "id": f"gcp-{item.get('id', '')}",
                "source_name": "GCP",
                "service_product": raw_service,
                "region": "Global",
                "start_date": start_date_str,
                "end_date": item.get("end", None),
                "duration_minutes": None,
                "severity": item.get("severity", "minor").lower(),
                "summary_en": summary,
                "official_url": official_url,
                "incident_updates": updates_list,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "hn_metrics": {"score": 0, "comments_count": 0},
                "_provenance_api": url
            }
            incidents.append(incident)
        return incidents
    except Exception as e:
        logger.error(f"[GCP] Erreur de décodage JSON : {e}")
        return None


def fetch_aws_incidents(session):
    """Extraction des incidents AWS via le flux RSS XML public."""
    url = "https://status.aws.amazon.com/rss/all.rss"
    incidents = []
    logger.info("Récupération des incidents AWS...")
    response = safe_get(session, url, "AWS")
    if response is None:
        return None

    try:
        root = ET.fromstring(response.content)
        for item in root.findall('./channel/item'):
            title = item.find('title').text if item.find('title') is not None else "AWS Incident"
            pub_date_str = item.find('pubDate').text if item.find('pubDate') is not None else ""
            desc = item.find('description').text if item.find('description') is not None else ""
            link = item.find('link').text if item.find('link') is not None else url

            try:
                dt_obj = parsedate_to_datetime(pub_date_str)
                start_date_iso = dt_obj.isoformat()
            except Exception:
                start_date_iso = datetime.now(timezone.utc).isoformat()

            clean_desc = clean_plain_excerpt(desc, 200) or "AWS Service Notification"
            status_label = "resolved" if "resolved" in title.lower() else "investigating"

            incident = {
                "id": f"aws-{title.replace(' ', '-').lower()[:30]}",
                "source_name": "AWS",
                "service_product": title.split('(')[0].strip() if '(' in title else "AWS Core Services",
                "region": "Global",
                "start_date": start_date_iso,
                "end_date": None,
                "duration_minutes": None,
                "severity": "minor" if status_label == "resolved" else "major",
                "summary_en": clean_desc,
                "official_url": link,
                "incident_updates": [
                    {
                        "status": status_label,
                        "timestamp": start_date_iso,
                        "summary_en": clean_plain_excerpt(clean_desc, MAX_UPDATE_TEXT_CHARS)
                    }
                ],
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "hn_metrics": {"score": 0, "comments_count": 0},
                "_provenance_api": url
            }
            incidents.append(incident)
        return incidents
    except Exception as e:
        logger.error(f"[AWS] Erreur d'analyse XML RSS : {e}")
        return None


def fetch_atlassian_statuspage(session, source_name, url):
    """
    Interroge les API publiques Atlassian Statuspage v2 (lecture seule, sans authentification).
    Extrait l'historique chronologique `incident_updates` (statut, horodatage display_at, résumé court).
    [À VÉRIFIER : Schéma officiel Statuspage v2 sur https://developer.statuspage.io/#operation/getPagesPageIdIncidents]
    """
    incidents = []
    logger.info(f"Récupération des incidents {source_name} (avec incident_updates)...")
    response = safe_get(session, url, source_name)
    if response is None:
        return None

    try:
        data = response.json()
        for item in data.get("incidents", []):
            start_date_str = item.get("created_at", "")
            raw_updates = item.get("incident_updates", []) or []
            updates_list = []

            for upd in raw_updates[:MAX_UPDATES_PER_INCIDENT]:
                upd_status = str(upd.get("status", "investigating")).lower()
                upd_time = upd.get("display_at") or upd.get("created_at") or start_date_str
                upd_body = clean_plain_excerpt(upd.get("body", ""), MAX_UPDATE_TEXT_CHARS)
                if upd_body:
                    updates_list.append({
                        "status": upd_status,
                        "timestamp": upd_time,
                        "summary_en": upd_body
                    })

            incident_name = clean_plain_excerpt(item.get("name", ""), 200) or f"{source_name} Service Incident"
            if not updates_list:
                updates_list.append({
                    "status": str(item.get("status", "investigating")).lower(),
                    "timestamp": start_date_str,
                    "summary_en": clean_plain_excerpt(incident_name, MAX_UPDATE_TEXT_CHARS)
                })

            incident = {
                "id": f"{source_name.lower()}-{item.get('id', '')}",
                "source_name": source_name,
                "service_product": f"{source_name} Platform" if source_name else "Platform",
                "region": "Global",
                "start_date": start_date_str,
                "end_date": item.get("resolved_at", None),
                "duration_minutes": None,
                "severity": item.get("impact", "minor").lower(),
                "summary_en": incident_name,
                "official_url": item.get("shortlink") or url,
                "incident_updates": updates_list,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "hn_metrics": {"score": 0, "comments_count": 0},
                "_provenance_api": url
            }
            incidents.append(incident)
        return incidents
    except Exception as e:
        logger.error(f"[{source_name}] Erreur de décodage JSON Statuspage : {e}")
        return None


def fetch_hackernews_outages(session):
    """Recherche des mentions communautaires majeures de pannes sur l'API publique Algolia HN."""
    url = "https://hn.algolia.com/api/v1/search_by_date"
    params = {
        "query": "outage OR downtime",
        "tags": "story",
        "numericFilters": "points>50",
        "hitsPerPage": 20
    }
    incidents = []
    logger.info("Récupération des mentions Hacker News...")
    response = safe_get(session, url, "HackerNews", params=params)
    if response is None:
        return None

    try:
        data = response.json()
        for item in data.get("hits", []):
            title_clean = clean_plain_excerpt(item.get("title", ""), 200)
            created_at = item.get("created_at", "")
            incident = {
                "id": f"hn-{item.get('objectID', '')}",
                "source_name": "HackerNews",
                "service_product": "Community Report",
                "region": "Global",
                "start_date": created_at,
                "end_date": None,
                "duration_minutes": None,
                "severity": "info",
                "summary_en": title_clean,
                "official_url": f"https://news.ycombinator.com/item?id={item.get('objectID')}",
                "incident_updates": [],
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "hn_metrics": {
                    "score": item.get("points", 0),
                    "comments_count": item.get("num_comments", 0)
                },
                "_provenance_api": f"{url}?query=outage"
            }
            incidents.append(incident)
        return incidents
    except Exception as e:
        logger.error(f"[HackerNews] Erreur de décodage JSON Algolia : {e}")
        return None


def process_and_deduplicate(incidents, max_age_days):
    """
    Élimine les doublons, nettoie les dates, calcule les durées en minutes
    et préserve la liste `incident_updates`.
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    unique_incidents = {}

    for inc in incidents:
        try:
            inc_date = datetime.fromisoformat(inc["start_date"].replace('Z', '+00:00'))
            if inc_date.tzinfo is None:
                inc_date = inc_date.replace(tzinfo=timezone.utc)

            if inc_date >= cutoff_date:
                if inc.get("end_date"):
                    end_date = datetime.fromisoformat(inc["end_date"].replace('Z', '+00:00'))
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                    duration = end_date - inc_date
                    inc["duration_minutes"] = max(0, int(duration.total_seconds() / 60))

                if "incident_updates" not in inc or not isinstance(inc["incident_updates"], list):
                    inc["incident_updates"] = []

                existing = unique_incidents.get(inc["id"])
                if not existing or (inc.get("end_date") and not existing.get("end_date")):
                    unique_incidents[inc["id"]] = inc
        except Exception as e:
            logger.debug(f"Avertissement de formatage pour l'incident {inc.get('id')} : {e}")

    return sorted(
        list(unique_incidents.values()),
        key=lambda x: x["start_date"],
        reverse=True
    )


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    session = create_resilient_session()

    all_incidents = []
    critical_sources_succeeded = 0
    critical_sources_total = 4

    critical_fetchers = [
        ("GCP", lambda: fetch_gcp_incidents(session)),
        ("AWS", lambda: fetch_aws_incidents(session)),
        ("GitHub", lambda: fetch_atlassian_statuspage(session, "GitHub", "https://www.githubstatus.com/api/v2/incidents.json")),
        ("Cloudflare", lambda: fetch_atlassian_statuspage(session, "Cloudflare", "https://www.cloudflarestatus.com/api/v2/incidents.json")),
    ]

    for source_label, fetcher_fn in critical_fetchers:
        result = fetcher_fn()
        if result is not None:
            critical_sources_succeeded += 1
            all_incidents.extend(result)
            logger.info(f"[{source_label}] {len(result)} événements bruts extraits.")
        else:
            logger.warning(f"[{source_label}] Source indisponible. Dégradation gracieuse : le pipeline continue.")

    hn_result = fetch_hackernews_outages(session)
    if hn_result is not None:
        all_incidents.extend(hn_result)
        logger.info(f"[HackerNews] {len(hn_result)} discussions communautaires extraites.")
    else:
        logger.warning("[HackerNews] Source communautaire indisponible. Ignorée.")

    if critical_sources_succeeded == 0:
        logger.critical(
            f"ÉCHEC CRITIQUE : 0/{critical_sources_total} sources d'infrastructure ont répondu. "
            f"Arrêt immédiat (exit code 1)."
        )
        sys.exit(1)

    clean_incidents = process_and_deduplicate(all_incidents, MAX_AGE_DAYS)

    output_data = {
        "_comment": "Généré automatiquement par scripts/ingest_outages.py (Schéma enrichi avec incident_updates)",
        "metadata": {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "total_incidents": len(clean_incidents),
            "critical_sources_ok": f"{critical_sources_succeeded}/{critical_sources_total}"
        },
        "incidents": clean_incidents
    }

    with open(OUTAGES_FILE, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    logger.info(
        f"Pipeline d'ingestion complété ({critical_sources_succeeded}/{critical_sources_total} sources critiques OK). "
        f"{len(clean_incidents)} incidents (avec incident_updates) sauvegardés dans {OUTAGES_FILE}."
    )


if __name__ == "__main__":
    main()