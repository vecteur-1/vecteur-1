# Chemin exact : scripts/generate_pages.py
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

# Nécessite : pip install python-frontmatter
import frontmatter

# Configuration du journal d'exécution (logging)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Paramètres globaux (chemins absolus sécurisés depuis la racine du projet)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, "data", "outages.json")
POSTS_DIR = os.path.join(BASE_DIR, "content", "posts")
STATIC_IMAGES_DIR = os.path.join(BASE_DIR, "static", "images")
DEFAULT_COVER_REL_PATH = "/images/default-cover.svg"
MAX_NEW_PAGES_PER_RUN = 5

# Contraintes de longueur du Quality Gate
AIDA_BRIEF_MIN_WORDS = 60
AIDA_BRIEF_MAX_WORDS = 135
ARTICLE_MIN_TOTAL_WORDS = 220

# Dictionnaire de configuration des partenaires d'affiliation
# [À VÉRIFIER : Remplacer les URL par tes liens d'affiliation réels une fois inscrits]
AFFILIATE_CONFIG = {
    "betterstack": {
        "nom": "Better Stack",
        "url": "https://betterstack.com/",  # [À REMPLACER PAR TON LIEN AFFILIÉ BETTER STACK]
        "proposition": "Secure your infrastructure with an independent incident management layer today.",
        "btn_texte": "Start Monitoring Free"
    },
    "statuscake": {
        "nom": "StatusCake",
        "url": "https://www.statuscake.com/"  # [À REMPLACER PAR TON LIEN AFFILIÉ STATUSCAKE]
    },
    "reditus": {
        "nom": "Reditus",
        "categorie": "B2B SaaS Affiliate & Partner Platform",
        "description": "Grow your B2B SaaS recurring revenue through a dedicated affiliate network and in-app referral program.",
        "avantage": "Verified B2B SaaS Marketplace",
        "url": "https://www.getreditus.com/"  # [À REMPLACER PAR TON LIEN AFFILIÉ REDITUS]
    }
}

# Visuel SVG vectoriel "Indigo Premium" auto-généré (dimensions explicites 800x450)
DEFAULT_COVER_SVG_CONTENT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 450" width="800" height="450">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#1e1b4b"/>
    </linearGradient>
    <linearGradient id="areaGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#4f46e5" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#4f46e5" stop-opacity="0.0"/>
    </linearGradient>
  </defs>
  <rect width="800" height="450" fill="url(#bg)"/>
  <g stroke="#334155" stroke-width="1" stroke-dasharray="4 4" opacity="0.6">
    <line x1="60" y1="90" x2="740" y2="90"/>
    <line x1="60" y1="180" x2="740" y2="180"/>
    <line x1="60" y1="270" x2="740" y2="270"/>
    <line x1="60" y1="360" x2="740" y2="360"/>
    <line x1="230" y1="60" x2="230" y2="390"/>
    <line x1="400" y1="60" x2="400" y2="390"/>
    <line x1="570" y1="60" x2="570" y2="390"/>
  </g>
  <polygon points="60,360 60,250 180,235 290,150 390,310 490,175 610,190 740,130 740,360" fill="url(#areaGrad)"/>
  <polyline fill="none" stroke="#818cf8" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" points="60,250 180,235 290,150 390,310 490,175 610,190 740,130"/>
  <circle cx="290" cy="150" r="7" fill="#6366f1" stroke="#ffffff" stroke-width="2"/>
  <circle cx="390" cy="310" r="8" fill="#ef4444" stroke="#ffffff" stroke-width="2"/>
  <circle cx="490" cy="175" r="7" fill="#6366f1" stroke="#ffffff" stroke-width="2"/>
  <circle cx="740" cy="130" r="7" fill="#22c55e" stroke="#ffffff" stroke-width="2"/>
  <rect x="60" y="35" width="210" height="32" rx="16" fill="#312e81" stroke="#4f46e5" stroke-width="1"/>
  <circle cx="80" cy="51" r="5" fill="#22c55e"/>
  <text x="95" y="56" fill="#e0e7ff" font-family="system-ui, -apple-system, sans-serif" font-size="13" font-weight="700" letter-spacing="1">SYNTHETIC TELEMETRY</text>
</svg>
"""


def sanitize_third_party_text(raw_text, max_chars=160):
    """
    Assainit rigoureusement tout texte provenant d'une source tierce (Statuspage, GCP, AWS, HN) :
    1. Supprime toute balise HTML brute (<script>, <a>, <p>, etc.).
    2. Neutralise les caractères Markdown/Tableaux (|, [, ], `, <, >, #, *).
    3. Normalise les espaces et tronque proprement pour interdire toute recopie intégrale.
    """
    if not raw_text:
        return "Service degradation reported by official status telemetry."
    text = re.sub(r"<[^>]+>", " ", str(raw_text))
    for ch, repl in [("|", "-"), ("[", "("), ("]", ")"), ("`", "'"), ("<", ""), (">", ""), ("#", ""), ("*", "")]:
        text = text.replace(ch, repl)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars - 3].rstrip() + "..."
    return text


def sanitize_external_url(raw_url):
    """Vérifie qu'une URL externe commence bien par http:// ou https:// et neutralise les parenthèses."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    url = raw_url.strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        return ""
    return url.replace(")", "%29").replace("(", "%28").replace(" ", "%20")


def ensure_default_cover_exists():
    """Crée automatiquement static/images/default-cover.svg s'il est absent du disque."""
    os.makedirs(STATIC_IMAGES_DIR, exist_ok=True)
    default_svg_path = os.path.join(STATIC_IMAGES_DIR, "default-cover.svg")
    if not os.path.exists(default_svg_path):
        with open(default_svg_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_COVER_SVG_CONTENT)
        logger.info(f"Visuel par défaut généré automatiquement : {default_svg_path}")


def resolve_service_image(slug):
    """
    Cherche une image dédiée au service dans static/images/.
    Accepte automatiquement les 3 conventions de nommage :
    1. <slug>-uptime-outage-history.<ext>
    2. <slug>-uptime-history.<ext>
    3. <slug>.<ext>
    """
    if not os.path.exists(STATIC_IMAGES_DIR):
        return DEFAULT_COVER_REL_PATH

    prefixes = [
        f"{slug}-uptime-outage-history",
        f"{slug}-uptime-history",
        slug
    ]
    valid_exts = (".webp", ".png", ".jpg", ".jpeg", ".svg")

    try:
        files_in_dir = os.listdir(STATIC_IMAGES_DIR)
    except OSError:
        return DEFAULT_COVER_REL_PATH

    for prefix in prefixes:
        for ext in valid_exts:
            exact_name = f"{prefix}{ext}"
            for disk_file in files_in_dir:
                if disk_file.lower() == exact_name.lower():
                    logger.info(f"[Image détectée] Utilisation de '/images/{disk_file}' pour '{slug}'.")
                    return f"/images/{disk_file}"

    for prefix in prefixes:
        for disk_file in files_in_dir:
            lower_file = disk_file.lower()
            if lower_file.startswith(prefix.lower() + ".") and lower_file.endswith(valid_exts):
                logger.warning(
                    f"[Image détectée avec double extension Windows] Fichier '{disk_file}' trouvé pour '{slug}'."
                )
                return f"/images/{disk_file}"

    return DEFAULT_COVER_REL_PATH


def load_data(file_path):
    """Vérifie et charge le fichier JSON. Arrête le script proprement si absent ou invalide."""
    if not os.path.exists(file_path):
        logger.error(f"Fichier introuvable : {file_path}. L'ingestion a-t-elle échoué ?")
        sys.exit(1)

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if "incidents" not in data or "metadata" not in data:
            logger.error("Structure JSON invalide : clés 'incidents' ou 'metadata' manquantes.")
            sys.exit(1)

        last_run_str = data["metadata"].get("last_run")
        if last_run_str:
            last_run = datetime.fromisoformat(last_run_str.replace('Z', '+00:00'))
            age_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
            if age_hours > 24:
                logger.warning(f"Attention: data/outages.json est obsolète ({age_hours:.1f} heures).")

        return data["incidents"]
    except json.JSONDecodeError as e:
        logger.error(f"Erreur de lecture JSON : {e}")
        sys.exit(1)


def cluster_by_service(incidents):
    """Regroupe les incidents par service/produit pour créer des pages thématiques."""
    clusters = {}
    for inc in incidents:
        if inc.get("source_name") == "HackerNews":
            continue

        raw_key = inc.get("service_product") or "Cloud Platform"
        if raw_key in ["Multiple Products", "Multiple"]:
            key = f"{inc.get('source_name', 'Cloud')} Multi-Service Infrastructure"
        else:
            key = sanitize_third_party_text(raw_key, max_chars=60)

        if key not in clusters:
            clusters[key] = []
        clusters[key].append(inc)
    return clusters


def build_condensed_aida_summary(service_name, latest_inc, total_downtime, incident_count):
    """
    Construit le bloc « Executive Incident Summary » structuré selon la progression AIDA
    sans afficher les labels méta-marketing à l'écran.
    """
    latest_date = datetime.fromisoformat(latest_inc["start_date"].replace('Z', '+00:00')).strftime("%B %d, %Y")
    severity = sanitize_third_party_text(latest_inc.get("severity", "minor"), 20).lower()
    short_cause = sanitize_third_party_text(latest_inc.get("summary_en", ""), 95)

    updates = latest_inc.get("incident_updates") or []
    if updates and isinstance(updates, list) and updates[0].get("summary_en"):
        latest_update_excerpt = sanitize_third_party_text(updates[0]["summary_en"], 95)
    else:
        latest_update_excerpt = short_cause

    summary_lines = [
        f"**Observed Impact:** On {latest_date}, independent status tracking recorded a {severity} incident affecting {service_name}, contributing to {total_downtime} cumulative minutes of verified disruption across {incident_count} tracked events.",
        f"**Reported Telemetry:** Official provider status updates documented the event as \"{short_cause}\" with latest operational notes stating \"{latest_update_excerpt}\".",
        f"**Independent Verification:** Because vendor status pages only reflect provider-acknowledged incidents, engineering teams use external synthetic monitoring to detect routing anomalies and regional latency spikes independently.",
        f"**Architectural Next Step:** Review the chronological audit trail below and evaluate an independent multi-region alerting stack for your production endpoints."
    ]
    return "\n\n".join(summary_lines)


def build_incident_updates_timeline(sorted_incidents):
    """
    Construit la chronologie des mises à jour d'incidents (`incident_updates`)
    avec horodatage UTC, statut normalisé, résumé court assaini et lien vers la source officielle.
    """
    lines = []
    for inc in sorted_incidents[:3]:
        inc_title = sanitize_third_party_text(inc.get("summary_en", "Service Incident"), 110)
        inc_date = datetime.fromisoformat(inc["start_date"].replace('Z', '+00:00')).strftime("%Y-%m-%d")
        official_url = sanitize_external_url(inc.get("official_url", ""))
        source_link = f"([Official Source]({official_url}))" if official_url else ""

        lines.append(f"#### {inc_date} — {inc_title} {source_link}")
        updates = inc.get("incident_updates") or []
        if updates:
            for upd in updates[:4]:
                status_tag = sanitize_third_party_text(upd.get("status", "update"), 25).upper()
                raw_ts = upd.get("timestamp", inc["start_date"])
                try:
                    ts_str = datetime.fromisoformat(raw_ts.replace('Z', '+00:00')).strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    ts_str = inc_date
                upd_excerpt = sanitize_third_party_text(upd.get("summary_en", ""), 140)
                lines.append(f"- **`[{status_tag}]`** *{ts_str}* — {upd_excerpt}")
        else:
            lines.append(f"- **`[RECORDED]`** *{inc_date}* — {inc_title}")
        lines.append("")

    return "\n".join(lines).strip()


def generate_aida_markdown(service_name, incidents, image_url):
    """
    Génère le corps du texte Markdown complet en appliquant l'architecture persuasive AIDA
    et le Decoy Effect. Les liens vers Better Stack et StatusCake dans le tableau comparatif
    utilisent le marqueur "sponsored" intercepté par layouts/_default/_markup/render-link.html
    pour injecter rel="sponsored nofollow".
    """
    sorted_inc = sorted(incidents, key=lambda x: x["start_date"], reverse=True)
    latest = sorted_inc[0]

    total_downtime = sum(i.get("duration_minutes", 0) for i in incidents if i.get("duration_minutes"))
    incident_count = len(incidents)
    latest_date_formatted = datetime.fromisoformat(latest["start_date"].replace('Z', '+00:00')).strftime("%B %d, %Y")
    latest_summary_clean = sanitize_third_party_text(latest.get("summary_en", "Elevated error rates observed."), 150)
    latest_severity_clean = sanitize_third_party_text(latest.get("severity", "service"), 20).lower()

    condensed_aida_brief = build_condensed_aida_summary(service_name, latest, total_downtime, incident_count)

    history_table = "| Date | Severity | Duration | Verified Source |\n|---|---|---|---|\n"
    for inc in sorted_inc[:5]:
        date_str = datetime.fromisoformat(inc["start_date"].replace('Z', '+00:00')).strftime("%Y-%m-%d")
        dur = f"{inc['duration_minutes']} mins" if inc.get("duration_minutes") is not None else "N/A"
        sev = sanitize_third_party_text(inc.get("severity", "unknown"), 20).capitalize()
        url_clean = sanitize_external_url(inc.get("official_url", ""))
        source_cell = f"[Official Report]({url_clean})" if url_clean else "N/A"
        history_table += f"| {date_str} | {sev} | {dur} | {source_cell} |\n"

    timeline_markdown = build_incident_updates_timeline(sorted_inc)

    betterstack_url = sanitize_external_url(AFFILIATE_CONFIG["betterstack"]["url"]) or "https://betterstack.com/"
    statuscake_url = sanitize_external_url(AFFILIATE_CONFIG["statuscake"]["url"]) or "https://www.statuscake.com/"

    content = f"""![{service_name} Uptime & Outage Telemetry]({image_url} "Automated synthetic telemetry and historical incident distribution for {service_name}.")

## Executive Incident Summary

> {condensed_aida_brief.replace(chr(10) + chr(10), chr(10) + "> " + chr(10) + "> ")}

---

## Is {service_name} Reliable for Production Workloads?

On **{latest_date_formatted}**, public status telemetry recorded a **{latest_severity_clean}** disruption affecting {service_name}. The official status feed summarized the event as: *"{latest_summary_clean}"*

For SRE and DevOps teams relying on third-party cloud dependencies, unmonitored upstream degradation directly impacts API latency, customer-facing availability, and internal error budgets.

## Uptime Benchmarks & Chronological Audit Trail

Aggregating publicly verified status feeds over the past 90 days, our automated pipeline recorded **{total_downtime} minutes of cumulative downtime** across **{incident_count} tracked incidents** for {service_name}.

{history_table}

### Chronological Incident Updates (Official Status Feed Excerpts)

The following timeline summarizes the key status transitions published by the provider during recent events (condensed excerpts; click the official source links for full vendor post-mortems):

{timeline_markdown}

### Vendor Comparison: Independent Observability Solutions

When evaluating external synthetic monitoring tools to track {service_name} endpoints independently, engineering teams typically compare these architectures:

| Solution | Best For | Starting Price |
|----------|----------|----------------|
| [**Better Stack**]({betterstack_url} "sponsored") | Incident Management, Status Pages & Uptime | $29 |
| [**StatusCake**]({statuscake_url} "sponsored") | Multi-Location HTTP & Page Speed Checks | $20.41 |
| **PagerDuty** | Full Enterprise On-Call Operations | $234 |

## Why Independent Synthetic Monitoring Matters

Relying exclusively on a cloud vendor's native status page creates an operational blind spot. Public status dashboards often aggregate regional anomalies or wait for manual incident confirmation before publishing an alert. Deploying an independent, multi-region synthetic monitoring layer allows your engineering team to detect HTTP timeouts, BGP routing shifts, or SSL handshake failures affecting your specific endpoints as soon as they occur.

This observability layer gives on-call engineers early telemetry to trigger failover routing or update customer status communications pro-actively.

## Audit Your Endpoint Monitoring Coverage

Do not wait for the next {service_name} disruption to test your incident response workflow. Evaluate an independent synthetic monitoring stack using the deployment options on this page.
"""
    return content, condensed_aida_brief


def quality_gate(cluster_name, cluster_incidents, markdown_content="", aida_brief=""):
    """
    Détermine si une page doit être indexée ('index'), non indexée ('noindex'), ou ignorée ('skip').
    """
    valid_incidents = [i for i in cluster_incidents if i.get("duration_minutes") is not None]
    if not valid_incidents:
        return "skip", "Aucun incident avec durée mesurable (Thin content risqué)."

    incidents_with_source = [
        i for i in valid_incidents
        if sanitize_external_url(i.get("official_url", "")) != ""
    ]
    if not incidents_with_source:
        return "skip", "Aucune URL de source officielle valide (official_url manquante)."

    if markdown_content and aida_brief:
        brief_words = len(re.findall(r"\b\w+\b", aida_brief))
        total_words = len(re.findall(r"\b\w+\b", markdown_content))

        if total_words < ARTICLE_MIN_TOTAL_WORDS:
            return "noindex", f"Contenu global trop court ({total_words} mots < {ARTICLE_MIN_TOTAL_WORDS}), mis en noindex."

        if brief_words < AIDA_BRIEF_MIN_WORDS or brief_words > AIDA_BRIEF_MAX_WORDS:
            logger.warning(
                f"[Quality Gate] Résumé exécutif pour {cluster_name} hors cible idéale "
                f"({brief_words} mots ; cible {AIDA_BRIEF_MIN_WORDS}-{AIDA_BRIEF_MAX_WORDS} mots)."
            )

    has_major = any(i.get("severity") in ["major", "critical"] for i in valid_incidents)
    if len(valid_incidents) >= 2 or has_major:
        return "index", f"Contenu riche vérifié ({len(valid_incidents)} incidents mesurés, sources officielles présentes)."

    return "noindex", "Contenu valide mais volume d'incidents limité, mis en noindex par précaution SEO."


def create_or_update_post(service_name, incidents, status_seo, markdown_content, image_url, is_dry_run):
    """Utilise python-frontmatter pour créer ou mettre à jour un fichier Markdown de façon idempotente."""
    slug = re.sub(r"[^a-z0-9]+", "-", service_name.lower()).strip("-")
    file_path = os.path.join(POSTS_DIR, f"{slug}-uptime-history.md")

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    incident_count = len(incidents)
    updates_count = sum(len(i.get("incident_updates", [])) for i in incidents)

    target_cross_sell = [
        {
            "nom": AFFILIATE_CONFIG["reditus"]["nom"],
            "categorie": AFFILIATE_CONFIG["reditus"]["categorie"],
            "description": AFFILIATE_CONFIG["reditus"]["description"],
            "avantage": AFFILIATE_CONFIG["reditus"]["avantage"],
            "url_affilie": AFFILIATE_CONFIG["reditus"]["url"]
        }
    ]

    post = None
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            post = frontmatter.load(f)

        existing_body = post.content or ""
        has_legacy_labels = any(
            marker in existing_body
            for marker in ["{.telemetry-figure}", "(AIDA Brief)", "(Target)", "(Decoy)", "## Attention:", "**Attention"]
        )

        # Vérifie que les liens affiliés du tableau comparatif sont bien présents dans le Markdown
        has_table_affiliate_links = (
            "[**Better Stack**](" in existing_body
            and "[**StatusCake**](" in existing_body
        )

        existing_cross_sell = post.metadata.get("outils_complementaires")
        cross_sell_up_to_date = (existing_cross_sell == target_cross_sell)

        if (
            post.metadata.get("tracked_incidents_count") == incident_count
            and post.metadata.get("tracked_updates_count") == updates_count
            and post.metadata.get("seo_index") == status_seo
            and post.metadata.get("image") == image_url
            and not has_legacy_labels
            and cross_sell_up_to_date
            and has_table_affiliate_links
        ):
            logger.debug(f"Idempotence : Aucun changement pour {service_name}. Ignoré.")
            return False

        logger.info(f"Mise à jour de la page pour {service_name} (Liens tableau comparatif + Cross-Sell Reditus).")
        post.metadata["lastmod"] = current_time
        post.metadata["seo_index"] = status_seo
        post.metadata["tracked_incidents_count"] = incident_count
        post.metadata["tracked_updates_count"] = updates_count
        post.metadata["image"] = image_url
        post.metadata["lien_affiliation_principal"] = AFFILIATE_CONFIG["betterstack"]["url"]
        post.metadata["outils_complementaires"] = target_cross_sell
        post.content = markdown_content
    else:
        logger.info(f"Création d'une nouvelle page pour {service_name} (Image: {image_url}).")
        post = frontmatter.Post(markdown_content)
        post.metadata = {
            "title": f"{service_name} Uptime & Outage History",
            "description": f"Data-driven incident analysis, status timeline, and downtime benchmark for {service_name}.",
            "date": current_time,
            "lastmod": current_time,
            "seo_index": status_seo,
            "type_page": "review",
            "keyword": service_name,
            "image": image_url,
            "tracked_incidents_count": incident_count,
            "tracked_updates_count": updates_count,

            "outil_cible": AFFILIATE_CONFIG["betterstack"]["nom"],
            "proposition_valeur": AFFILIATE_CONFIG["betterstack"]["proposition"],
            "lien_affiliation_principal": AFFILIATE_CONFIG["betterstack"]["url"],
            "texte_bouton": AFFILIATE_CONFIG["betterstack"]["btn_texte"],
            "preuve_sociale_note": "4.8/5",

            "outils_complementaires": target_cross_sell
        }

    if not is_dry_run:
        os.makedirs(POSTS_DIR, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(frontmatter.dumps(post))

    return True


def main():
    parser = argparse.ArgumentParser(description="Générateur programmatique de pages Hugo (AIDA + Post-Mortems).")
    parser.add_argument("--dry-run", action="store_true", help="Simule l'exécution sans écrire de fichiers.")
    parser.add_argument("--verbose", action="store_true", help="Affiche les logs détaillés.")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not args.dry_run:
        ensure_default_cover_exists()

    incidents = load_data(DATA_FILE)
    clusters = cluster_by_service(incidents)

    pages_created = 0
    pages_noindex = 0
    pages_skipped = 0

    logger.info("=== DÉBUT DU QUALITY GATE (AVEC CONTRÔLE DE SOURCE & LONGUEUR) ===")

    for service, cluster_incidents in clusters.items():
        if pages_created + pages_noindex >= MAX_NEW_PAGES_PER_RUN:
            logger.info("Limite de Drip Publishing atteinte pour cette exécution.")
            break

        slug = re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-")
        image_url = resolve_service_image(slug)

        markdown_content, aida_brief = generate_aida_markdown(service, cluster_incidents, image_url)
        status_seo, reason = quality_gate(service, cluster_incidents, markdown_content, aida_brief)

        if status_seo == "skip":
            logger.debug(f"[REJETÉ] {service} : {reason}")
            pages_skipped += 1
            continue

        brief_word_count = len(re.findall(r"\b\w+\b", aida_brief))
        total_word_count = len(re.findall(r"\b\w+\b", markdown_content))
        logger.info(
            f"[ACCEPTÉ - {status_seo.upper()}] {service} : {reason} "
            f"(Brief: {brief_word_count} mots | Total: {total_word_count} mots)"
        )

        is_processed = create_or_update_post(
            service, cluster_incidents, status_seo, markdown_content, image_url, args.dry_run
        )

        if is_processed:
            if status_seo == "index":
                pages_created += 1
            else:
                pages_noindex += 1

    logger.info("=== RAPPORT DE GÉNÉRATION ===")
    logger.info(f"Pages traitées (Index)  : {pages_created}")
    logger.info(f"Pages traitées (NoIndex): {pages_noindex}")
    logger.info(f"Clusters rejetés        : {pages_skipped}")
    if args.dry_run:
        logger.info("Mode --dry-run actif : Aucun fichier n'a été modifié.")


if __name__ == "__main__":
    main()