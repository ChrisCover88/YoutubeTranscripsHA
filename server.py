import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict
import uvicorn
from youtube_transcript_api import YouTubeTranscriptApi

# Importiert die moderne MCPServer-Klasse (aus mcp 2.x)
try:
    from mcp.server.mcpserver import MCPServer as ServerClass
except ImportError:
    from mcp.server.fastmcp import FastMCP as ServerClass

# ==========================================
# 1. SERVER-KONFIGURATION
# ==========================================

# Render vergibt den Port automatisch über die Umgebungsvariable PORT
PORT = int(os.environ.get("PORT", 8000))

# Initialisiere den MCP-Server mit Host und Port für die Cloud
mcp = ServerClass("YouTube HA-Rückblick Konnektor", host="0.0.0.0", port=PORT)

# Deine 9 definierten YouTube-Kanäle für den wöchentlichen Rückblick
HA_CHANNELS: Dict[str, str] = {
    "Quelle 1": "@alles-automatisch",
    "Quelle 2": "@simon42",
    "Quelle 3": "@smarthome_more",
    "Quelle 4": "@smarthomeyourself",
    "Quelle 5": "@flotomation",
    "Quelle 6": "@tristanssmartesheim",
    "Quelle 7": "@smarterkram",
    "Quelle 8": "@haus_automation",
    "Quelle 9": "@CTechMedia"
}

# Zwischenspeicher (Cache) für die ermittelten YouTube Channel-IDs
CHANNEL_ID_CACHE: Dict[str, str] = {}

# Cookie-Consent-Header umgeht die europäische YouTube-Vorschaltseite
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
    'Cookie': 'CONSENT=YES+cb.20230531-07-p0.de+FX+999'
}


# ==========================================
# 2. HILFSFUNKTIONEN
# ==========================================

def fetch_channel_html(handle: str) -> str:
    """Ruft den HTML-Quelltext einer YouTube-Kanalseite ab."""
    clean_handle = handle.strip()
    if not clean_handle.startswith("@"):
        clean_handle = "@" + clean_handle
        
    url = f"https://www.youtube.com/{clean_handle}"
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=12).read().decode('utf-8', errors='ignore')


def resolve_channel_id(handle: str) -> str:
    """Ermittelt die interne YouTube Channel-ID ('UC...') für den RSS-Feed."""
    if handle in CHANNEL_ID_CACHE:
        return CHANNEL_ID_CACHE[handle]
        
    html = fetch_channel_html(handle)
    match = re.search(r'"externalId":"(UC[a-zA-Z0-9_-]{22})"', html)
    if match:
        CHANNEL_ID_CACHE[handle] = match.group(1)
        return match.group(1)
    raise ValueError(f"Channel-ID für {handle} konnte nicht ermittelt werden.")


# ==========================================
# 3. MCP-WERKZEUGE FÜR CLAUDE
# ==========================================

@mcp.tool()
def check_all_ha_channels(days: int = 7) -> str:
    """
    Prüft alle 9 definierten YouTube-Kanäle (Quelle 1 bis 9) exakt auf neue Videos
    innerhalb der letzten 'days' Tage (Standard: 7 Tage).
    Liefert auch eine eindeutige Negativ-Bestätigung für Kanäle ohne neue Videos.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(days=days)
    
    report_lines = [
        f"=== YOUTUBE-SCAN ERGEBNIS ({cutoff_utc.strftime('%d.%m.%Y')} bis {now_utc.strftime('%d.%m.%Y')}) ===\n"
    ]
    
    ns = {
        'atom': 'http://www.w3.org/2005/Atom',
        'yt': 'http://www.youtube.com/xml/schemas/2015',
        'media': 'http://search.yahoo.com/mrss/'
    }

    for source_label, handle in HA_CHANNELS.items():
        try:
            channel_id = resolve_channel_id(handle)
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            req = urllib.request.Request(rss_url, headers=HEADERS)
            xml_data = urllib.request.urlopen(req, timeout=10).read()
            root = ET.fromstring(xml_data)
            
            channel_title = root.find('atom:title', ns).text
            new_videos = []
            latest_outside_window = None
            
            for entry in root.findall('atom:entry', ns):
                pub_str = entry.find('atom:published', ns).text
                pub_dt = datetime.fromisoformat(pub_str)
                title = entry.find('atom:title', ns).text
                vid_id = entry.find('yt:videoId', ns).text
                
                if pub_dt >= cutoff_utc:
                    new_videos.append(
                        f"  ✅ NEUES VIDEO: '{title}'\n"
                        f"     Datum: {pub_dt.strftime('%d.%m.%Y %H:%M')} UTC\n"
                        f"     Video-ID: {vid_id}\n"
                        f"     Link: https://www.youtube.com/watch?v={vid_id}"
                    )
                elif latest_outside_window is None:
                    latest_outside_window = f"{title} (vom {pub_dt.strftime('%d.%m.%Y')})"

            report_lines.append(f"--- {source_label}: {channel_title} ({handle}) ---")
            if new_videos:
                report_lines.extend(new_videos)
            else:
                report_lines.append(
                    f"  ❌ NEGATIV-BESTÄTIGUNG: Keine neuen Videos im {days}-Tage-Fenster gefunden."
                )
                if latest_outside_window:
                    report_lines.append(f"     (Letztes Video außerhalb des Fensters: {latest_outside_window})")
            report_lines.append("")
            
        except Exception as e:
            report_lines.append(f"--- {source_label} ({handle}) ---")
            report_lines.append(f"  ⚠️ Technisch nicht erreichbar / Fehler: {str(e)}\n")

    return "\n".join(report_lines)


@mcp.tool()
def get_video_transcript(video_id: str) -> str:
    """
    Liest das gesprochene Transkript (Untertitel) eines YouTube-Videos anhand der Video-ID aus.
    """
    try:
        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(video_id, languages=['de', 'en'])
        full_text = " ".join([snippet.text for snippet in fetched])
        return f"Transkript für Video {video_id}:\n{full_text[:45000]}"
    except AttributeError:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['de', 'en'])
        full_text = " ".join([entry['text'] for entry in transcript_list])
        return f"Transkript für Video {video_id}:\n{full_text[:45000]}"
    except Exception as e:
        return f"Kein Transkript für Video-ID {video_id} abrufbar (Grund: {str(e)})."


@mcp.tool()
def find_channel_websites(channel_handle: str) -> str:
    """
    Untersucht den Kanal-Quelltext eines YouTube-Handles (z.B. für Quelle 4 bis 9) 
    nach verlinkten externen Websites, Blogs, Foren oder Discord-Servern.
    """
    try:
        html = fetch_channel_html(channel_handle)
        raw_redirects = re.findall(r'https://www\.youtube\.com/redirect\?event=channel_description&[^\s"\'<>]+', html)
        
        found_urls = set()
        for r_url in raw_redirects:
            q_match = re.search(r'[?&]q=([^&]+)', r_url)
            if q_match:
                decoded_url = urllib.parse.unquote(q_match.group(1))
                found_urls.add(decoded_url)
                
        if not found_urls:
            return f"Auf der Kanalseite von {channel_handle} konnten keine externen Website-Links extrahiert werden."
            
        return f"Gefundene Links für {channel_handle}:\n" + "\n".join([f"- {u}" for u in sorted(found_urls)])
    except Exception as e:
        return f"Fehler beim Auslesen der Links für {channel_handle}: {str(e)}"


# ==========================================
# 4. START MIT STREAMABLE HTTP (/mcp)
# ==========================================
if __name__ == "__main__":
    # Startet den Server im modernen 'streamable-http' Modus unter dem Pfad /mcp
    try:
        app = mcp.streamable_http_app()
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    except AttributeError:
        # Alternativer direkter Aufruf in MCP 2.x
        mcp.run(transport="streamable-http")
