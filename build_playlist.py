#!/usr/bin/env python3
"""
IPTV2 CURATED FINAL

Sources:
- Turkish: iptv-org Turkish language playlist
- Foreign: iptv-org English language playlist
- Four explicit user-provided Turkish overrides: manual_overrides.json

There is NO health check, resolver, automatic third-party fallback pool,
blacklist/whitelist or URL scoring.

For iptv-org channels the first matching entry in iptv-org's own order is used.
"""

import csv
import json
import re
import unicodedata
from pathlib import Path
from urllib.request import Request, urlopen

TR_SOURCE = "https://iptv-org.github.io/iptv/languages/tur.m3u"
EN_SOURCE = "https://iptv-org.github.io/iptv/languages/eng.m3u"

TR_CONFIG = Path("channels.json")
EN_CONFIG = Path("foreign_channels.json")
OVERRIDES = Path("manual_overrides.json")
DYNAMIC_SOURCE = Path("dynamic_source.json")
OUT_M3U = Path("turkiye-tv.m3u")
OUT_REPORT = Path("playlist-report.csv")

USER_AGENT = "Mozilla/5.0 (IPTV2-Curated-Reorder/3.0)"


def norm(text):
    text = (text or "").casefold().replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text)


def parse_attrs(extinf):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', extinf))


def fetch_text(url, timeout=90):
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/x-mpegURL,application/vnd.apple.mpegurl,text/plain,*/*"
    })
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def parse_m3u(text):
    lines = text.splitlines()
    entries = []
    i = 0
    order = 0

    while i < len(lines):
        line = lines[i].rstrip("\r")
        if not line.startswith("#EXTINF:"):
            i += 1
            continue

        extinf = line
        attrs = parse_attrs(extinf)
        visible_name = extinf.split(",", 1)[1].strip() if "," in extinf else ""
        directives = []
        url = ""

        j = i + 1
        while j < len(lines):
            nxt = lines[j].rstrip("\r")
            stripped = nxt.strip()
            if not stripped:
                j += 1
                continue
            if stripped.startswith("#EXTINF:"):
                break
            if stripped.startswith("#"):
                directives.append(nxt)
                j += 1
                continue
            url = stripped
            j += 1
            break

        if url:
            order += 1
            entries.append({
                "source_order": order,
                "extinf": extinf,
                "attrs": attrs,
                "name": visible_name,
                "tvg_name": attrs.get("tvg-name", ""),
                "tvg_id": attrs.get("tvg-id", ""),
                "directives": directives,
                "url": url,
            })

        i = max(j, i + 1)

    return entries


def base_tvg_id(value):
    return (value or "").split("@", 1)[0]


def find_entry(cfg, entries, use_base_ids=False):
    # Exact feed/channel IDs first, preserving source order.
    for wanted in cfg.get("ids", []):
        for entry in entries:
            if entry["tvg_id"] == wanted:
                return entry, f"exact:{wanted}"

    # Useful for English feeds where @Feed suffixes vary.
    if use_base_ids:
        wanted_bases = cfg.get("base_ids", []) or cfg.get("ids", [])
        for wanted in wanted_bases:
            wanted_base = base_tvg_id(wanted)
            for entry in entries:
                if base_tvg_id(entry["tvg_id"]) == wanted_base:
                    return entry, f"base:{wanted_base}"

    # Conservative name fallback.
    wanted_names = [cfg.get("name", "")] + cfg.get("aliases", [])
    wanted_norms = {norm(x) for x in wanted_names if norm(x)}
    for entry in entries:
        source_norms = {norm(entry.get("name", "")), norm(entry.get("tvg_name", ""))}
        if wanted_norms & source_norms:
            return entry, "name"

    return None, "not_found"


def rewrite_extinf(extinf, number, display_name, group=None):
    head, _old_name = extinf.split(",", 1) if "," in extinf else (extinf, "")

    if re.search(r'tvg-chno="[^"]*"', head):
        head = re.sub(r'tvg-chno="[^"]*"', f'tvg-chno="{number}"', head, count=1)
    else:
        head += f' tvg-chno="{number}"'

    if group:
        if re.search(r'group-title="[^"]*"', head):
            head = re.sub(r'group-title="[^"]*"', f'group-title="{group}"', head, count=1)
        else:
            head += f' group-title="{group}"'

    return f"{head},{display_name}"


def make_manual_extinf(cfg, override):
    tvg_id = (cfg.get("ids") or [f"custom.{norm(cfg['name'])}"])[0]
    logo = override.get("logo", "")
    group = override.get("group", "")
    parts = [
        '#EXTINF:-1',
        f'tvg-id="{tvg_id}"',
        f'tvg-chno="{cfg["number"]}"',
    ]
    if logo:
        parts.append(f'tvg-logo="{logo}"')
    if group:
        parts.append(f'group-title="{group}"')
    return " ".join(parts) + f',{cfg["name"]}'


def override_for(cfg, overrides):
    for cid in cfg.get("ids", []):
        base = base_tvg_id(cid)
        if cid in overrides and isinstance(overrides[cid], dict):
            return overrides[cid]
        if base in overrides and isinstance(overrides[base], dict):
            return overrides[base]
    return None


def dynamic_rule_for(cfg, dynamic_cfg):
    rules = (dynamic_cfg or {}).get("channels", {})
    for cid in cfg.get("ids", []):
        base = base_tvg_id(cid)
        if cid in rules:
            return rules[cid]
        if base in rules:
            return rules[base]
    return None


def find_dynamic_entry(rule, entries):
    if not rule:
        return None, "not_allowlisted"

    for wanted in rule.get("ids", []):
        for entry in entries:
            if entry["tvg_id"] == wanted:
                return entry, f"dynamic-exact:{wanted}"

    wanted_norms = {
        norm(x) for x in ([rule.get("name", "")] + rule.get("aliases", []))
        if norm(x)
    }
    for entry in entries:
        if wanted_norms & {norm(entry.get("name", "")), norm(entry.get("tvg_name", ""))}:
            return entry, "dynamic-name"

    return None, "dynamic-not-found"


def main():
    tr_cfg = json.loads(TR_CONFIG.read_text(encoding="utf-8"))["channels"]
    en_cfg = json.loads(EN_CONFIG.read_text(encoding="utf-8"))["channels"]
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    dynamic_cfg = json.loads(DYNAMIC_SOURCE.read_text(encoding="utf-8")) if DYNAMIC_SOURCE.exists() else {}

    dynamic_entries = []
    dynamic_error = ""
    dynamic_url = (dynamic_cfg or {}).get("url", "").strip()
    if dynamic_url:
        print(f"Downloading selected-channel dynamic source: {dynamic_url}")
        try:
            dynamic_entries = parse_m3u(fetch_text(dynamic_url))
            print(f"Dynamic source entries parsed: {len(dynamic_entries)}")
        except Exception as exc:
            dynamic_error = f"{type(exc).__name__}: {exc}"
            print(f"WARNING: dynamic source unavailable; using normal fallbacks: {dynamic_error}")

    print("Downloading iptv-org Turkish playlist...")
    tr_entries = parse_m3u(fetch_text(TR_SOURCE))
    print("Downloading iptv-org English playlist...")
    en_entries = parse_m3u(fetch_text(EN_SOURCE))

    print(f"Turkish configured: {len(tr_cfg)}")
    print(f"Foreign configured: {len(en_cfg)}")
    print("Mode: iptv-org + allowlisted dynamic TinyURL source + manual fallback")

    out = [
        "#EXTM3U",
        f"# Turkish source: {TR_SOURCE}",
        f"# Foreign source: {EN_SOURCE}",
        "# Dynamic selected-channel source + manual fallback: dynamic_source.json / manual_overrides.json"
    ]
    report = []

    tr_added = 0
    for position, cfg in enumerate(tr_cfg, start=1):
        entry, method = find_entry(cfg, tr_entries, use_base_ids=False)
        override = override_for(cfg, overrides)
        primary = (override or {}).get("primary", "").strip()

        # Priority 1: dynamic secondary M3U, but ONLY for explicitly allowlisted channels.
        dynamic_rule = dynamic_rule_for(cfg, dynamic_cfg)
        dynamic_entry, dynamic_method = find_dynamic_entry(dynamic_rule, dynamic_entries)

        if dynamic_entry:
            # Keep our configured visible name/channel number, but use the current URL
            # and any source directives from the dynamically downloaded M3U.
            out.append(rewrite_extinf(
                dynamic_entry["extinf"], cfg["number"], cfg["name"]
            ))
            out.extend(dynamic_entry["directives"])
            out.append(dynamic_entry["url"])
            tr_added += 1
            report.append({
                "section": "turkish",
                "position": position,
                "channel_number": cfg["number"],
                "wanted_name": cfg["name"],
                "status": "added_dynamic_source",
                "source": dynamic_url,
                "match_method": dynamic_method,
                "source_order": dynamic_entry["source_order"],
                "matched_tvg_id": dynamic_entry["tvg_id"],
                "source_name": dynamic_entry["name"],
                "url": dynamic_entry["url"],
            })
            continue

        # Priority 2: user's current hard-coded fallback, only for those same/manual channels.
        if primary:
            if entry:
                out.append(rewrite_extinf(entry["extinf"], cfg["number"], cfg["name"]))
                out.extend(entry["directives"])
            else:
                out.append(make_manual_extinf(cfg, override))
            out.append(primary)
            tr_added += 1
            report.append({
                "section": "turkish",
                "position": position,
                "channel_number": cfg["number"],
                "wanted_name": cfg["name"],
                "status": "added_manual_fallback",
                "source": "manual_overrides.json",
                "match_method": (dynamic_method + " -> " + method) if dynamic_rule else method,
                "source_order": entry["source_order"] if entry else "",
                "matched_tvg_id": entry["tvg_id"] if entry else (cfg.get("ids") or [""])[0],
                "source_name": entry["name"] if entry else cfg["name"],
                "url": primary,
            })
            continue

        if not entry:
            report.append({
                "section": "turkish",
                "position": position,
                "channel_number": cfg["number"],
                "wanted_name": cfg["name"],
                "status": "not_found_in_iptv_org_tur",
                "source": "iptv-org tur.m3u",
                "match_method": method,
                "source_order": "",
                "matched_tvg_id": "",
                "source_name": "",
                "url": "",
            })
            continue

        out.append(rewrite_extinf(entry["extinf"], cfg["number"], cfg["name"]))
        out.extend(entry["directives"])
        out.append(entry["url"])
        tr_added += 1
        report.append({
            "section": "turkish",
            "position": position,
            "channel_number": cfg["number"],
            "wanted_name": cfg["name"],
            "status": "added_iptv_org",
            "source": "iptv-org tur.m3u",
            "match_method": method,
            "source_order": entry["source_order"],
            "matched_tvg_id": entry["tvg_id"],
            "source_name": entry["name"],
            "url": entry["url"],
        })

    en_added = 0
    for position, cfg in enumerate(en_cfg, start=1):
        entry, method = find_entry(cfg, en_entries, use_base_ids=True)
        if not entry:
            report.append({
                "section": "foreign",
                "position": position,
                "channel_number": cfg["number"],
                "wanted_name": cfg["name"],
                "status": "not_found_in_iptv_org_eng",
                "source": "iptv-org eng.m3u",
                "match_method": method,
                "source_order": "",
                "matched_tvg_id": "",
                "source_name": "",
                "url": "",
            })
            continue

        out.append(rewrite_extinf(
            entry["extinf"], cfg["number"], cfg["name"], cfg.get("group")
        ))
        out.extend(entry["directives"])
        out.append(entry["url"])
        en_added += 1
        report.append({
            "section": "foreign",
            "position": position,
            "channel_number": cfg["number"],
            "wanted_name": cfg["name"],
            "status": "added_iptv_org",
            "source": "iptv-org eng.m3u",
            "match_method": method,
            "source_order": entry["source_order"],
            "matched_tvg_id": entry["tvg_id"],
            "source_name": entry["name"],
            "url": entry["url"],
        })

    OUT_M3U.write_text("\n".join(out) + "\n", encoding="utf-8")

    fields = [
        "section", "position", "channel_number", "wanted_name", "status",
        "source", "match_method", "source_order", "matched_tvg_id",
        "source_name", "url"
    ]
    with OUT_REPORT.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report)

    print(f"Turkish added: {tr_added}/{len(tr_cfg)}")
    print(f"Foreign added: {en_added}/{len(en_cfg)}")
    print(f"Wrote: {OUT_M3U} and {OUT_REPORT}")

    missing = [r for r in report if r["status"].startswith("not_found")]
    if missing:
        print("Unavailable from current iptv-org playlists:")
        for r in missing:
            print(f"  [{r['section']}] {r['wanted_name']}: {r['status']}")


if __name__ == "__main__":
    main()
