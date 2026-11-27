import json
import os
import re
import sys
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

APK_DIR = Path("apk")
EXTRACT_TMP_DIR = Path(".apk_extract_tmp")
STATE_FILE = Path(".apk_store_state.json")
README_FILE = Path("README.md")

START_MARKER = "<!-- APK_STORE_START -->"
END_MARKER = "<!-- APK_STORE_END -->"


def api_request(
    method: str,
    url: str,
    token: str,
    data: bytes | None = None,
    content_type: str = "application/json",
):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ApkStorePublisher/1.0",
    }
    if data is not None:
        headers["Content-Type"] = content_type

    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API error {exc.code} on {method} {url}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error on {method} {url}: {exc}") from exc


def normalize_asset_name(name: str) -> str:
    # GitHub asset upload sometimes normalizes spaces to dots
    p = Path(name)
    stem = re.sub(r"\s+", ".", p.stem).strip(".")
    return f"{stem}{p.suffix.lower()}"


def upload_release_asset(upload_url_template: str, token: str, apk_path: Path) -> str:
    base_upload_url = upload_url_template.split("{", 1)[0]
    asset_name = apk_path.name
    encoded_name = quote(asset_name, safe="")
    url = f"{base_upload_url}?name={encoded_name}"

    file_bytes = apk_path.read_bytes()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Length": str(len(file_bytes)),
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ApkStorePublisher/1.0",
    }

    def _post_asset(target_url: str) -> dict:
        req = Request(target_url, data=file_bytes, headers=headers, method="POST")
        with urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        payload = _post_asset(url)
        return payload["browser_download_url"]
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 422:
            parts = base_upload_url.rstrip("/").split("/")
            if "/releases/" in base_upload_url and len(parts) >= 2:
                release_id = parts[-2]
                owner_repo_part = base_upload_url.split("/repos/", 1)[1].split("/releases/", 1)[0]
                assets_url = f"https://api.github.com/repos/{owner_repo_part}/releases/{release_id}/assets"
                target_norm = normalize_asset_name(asset_name)

                try:
                    assets = api_request("GET", assets_url, token)
                    if isinstance(assets, list):
                        for asset in assets:
                            existing_name = str(asset.get("name") or "")
                            if (
                                existing_name == asset_name
                                or normalize_asset_name(existing_name) == target_norm
                            ) and asset.get("id") is not None:
                                del_url = f"{assets_url}/{asset['id']}"
                                try:
                                    api_request("DELETE", del_url, token)
                                except RuntimeError as del_exc:
                                    if "error 404 " not in str(del_exc):
                                        raise
                except RuntimeError as list_exc:
                    if "error 404 " not in str(list_exc):
                        raise

            try:
                payload2 = _post_asset(url)
                return payload2["browser_download_url"]
            except HTTPError as retry_exc:
                retry_body = retry_exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Asset retry upload failed ({retry_exc.code}): {retry_body}"
                ) from retry_exc

        raise RuntimeError(f"Asset upload failed ({exc.code}): {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error while uploading {asset_name}: {exc}") from exc


def get_or_create_release(repo_full_name: str, token: str, tag_name: str, release_title: str) -> dict:
    api_base = f"https://api.github.com/repos/{repo_full_name}"
    try:
        return api_request("GET", f"{api_base}/releases/tags/{tag_name}", token)
    except RuntimeError as exc:
        if "error 404 " not in str(exc):
            raise
        body = json.dumps({
            "tag_name": tag_name,
            "name": release_title,
            "body": f"Automated APK release for **{release_title}**.",
            "draft": False,
            "prerelease": False,
        }).encode("utf-8")
        return api_request("POST", f"{api_base}/releases", token, data=body)


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned or "app"


def default_title_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    readable = re.sub(r"[_\-]+", " ", stem).strip()
    return readable.title() if readable else "App"


def clean_bold_text(text: str) -> str:
    s = text.strip()
    s = re.sub(r"^\*+|\*+$", "", s).strip()
    return s


def parse_existing_readme_overrides(readme_text: str) -> dict[str, dict[str, str]]:
    overrides: dict[str, dict[str, str]] = {}
    if START_MARKER not in readme_text or END_MARKER not in readme_text:
        return overrides

    block = readme_text.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
    for line in block.splitlines():
        url_match = re.search(r"(https://github\.com/[^\s\)\]`|]+?\.apk)", line)
        if not url_match:
            continue
        url = url_match.group(1)

        if line.strip().startswith("|"):
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 2:
                raw_name = clean_bold_text(cols[0])
                raw_ver = re.sub(r"^`|`$", "", cols[1]).strip()
                entry: dict[str, str] = {}
                if raw_name and raw_name.lower() not in {"*no apks published yet*", "no apks published yet"}:
                    entry["name"] = raw_name
                if raw_ver and raw_ver != "-":
                    entry["version"] = raw_ver
                if entry:
                    overrides[url] = entry
    return overrides


def extract_apks_from_folder() -> list[Path]:
    APK_DIR.mkdir(parents=True, exist_ok=True)
    gitkeep = APK_DIR / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")

    EXTRACT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    found_apks: list[Path] = []

    for p in sorted(APK_DIR.rglob("*.apk")):
        if p.is_file():
            dest = EXTRACT_TMP_DIR / p.name
            if dest.exists():
                stem = Path(p.name).stem
                idx = 1
                while (EXTRACT_TMP_DIR / f"{stem}_{idx}.apk").exists():
                    idx += 1
                dest = EXTRACT_TMP_DIR / f"{stem}_{idx}.apk"
            dest.write_bytes(p.read_bytes())
            found_apks.append(dest)
            p.unlink(missing_ok=True)

    for zp in sorted(APK_DIR.rglob("*.zip")):
        if not zp.is_file():
            continue
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    rel = Path(member.filename)
                    if rel.is_absolute() or ".." in rel.parts:
                        continue
                    if rel.suffix.lower() == ".apk":
                        target_name = rel.name
                        dest = EXTRACT_TMP_DIR / target_name
                        if dest.exists():
                            stem = Path(target_name).stem
                            idx = 1
                            while (EXTRACT_TMP_DIR / f"{stem}_{idx}.apk").exists():
                                idx += 1
                            dest = EXTRACT_TMP_DIR / f"{stem}_{idx}.apk"
                        with zf.open(member) as src, open(dest, "wb") as dst:
                            dst.write(src.read())
                        found_apks.append(dest)
        except zipfile.BadZipFile:
            print(f"[SKIP INVALID ZIP] {zp}")
        finally:
            zp.unlink(missing_ok=True)

    return found_apks


def load_state() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_state(apps: list[dict]) -> None:
    STATE_FILE.write_text(json.dumps(apps, indent=2, ensure_ascii=False), encoding="utf-8")


def update_readme(apps: list[dict]) -> None:
    if README_FILE.exists():
        readme_text = README_FILE.read_text(encoding="utf-8")
    else:
        readme_text = "# My APK Store\n\n"

    overrides = parse_existing_readme_overrides(readme_text)

    lines = [
        START_MARKER,
        "## Available APKs",
        "",
        "| App Name | Version | Download | Copy Link |",
        "| :--- | :---: | :--- | :--- |",
    ]

    if not apps:
        lines.append("| ***No APKs published yet*** | `-` | *Upload `.apk` or `.zip` into `apk/`* | `-` |")
    else:
        for item in apps:
            url = item["download_url"]
            ov = overrides.get(url, {})
            raw_name = ov.get("name") or item["name"]
            app_name = clean_bold_text(raw_name) or "App"
            version = ov.get("version") or item.get("version") or "1.0"
            item["name"] = app_name
            item["version"] = version

            lines.append(
                f"| **{app_name}** | `{version}` | [Download APK]({url}) | `{url}` |"
            )

    lines.append("")
    lines.append(END_MARKER)
    new_block = "\n".join(lines)

    if START_MARKER in readme_text and END_MARKER in readme_text:
        pattern = re.compile(rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}", re.DOTALL)
        updated = pattern.sub(new_block, readme_text, count=1)
    else:
        sep = "\n\n" if not readme_text.endswith("\n\n") else ""
        updated = f"{readme_text}{sep}{new_block}\n"

    README_FILE.write_text(updated, encoding="utf-8")


def cleanup_tmp() -> None:
    if EXTRACT_TMP_DIR.exists():
        for f in EXTRACT_TMP_DIR.rglob("*"):
            if f.is_file():
                f.unlink(missing_ok=True)
        for d in sorted(EXTRACT_TMP_DIR.rglob("*"), reverse=True):
            if d.is_dir():
                d.rmdir()
        EXTRACT_TMP_DIR.rmdir()


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "").strip()

    if not token or not repo_full_name:
        print("Error: GITHUB_TOKEN and GITHUB_REPOSITORY environment variables are required.", file=sys.stderr)
        return 1

    APK_DIR.mkdir(parents=True, exist_ok=True)
    if not (APK_DIR / ".gitkeep").exists():
        (APK_DIR / ".gitkeep").write_text("", encoding="utf-8")

    apps = load_state()
    existing_by_url = {a["download_url"]: a for a in apps if "download_url" in a}

    new_apks = extract_apks_from_folder()
    try:
        for apk_path in new_apks:
            app_slug = slugify(apk_path.stem)
            tag_name = f"apk-{app_slug}"
            release_title = default_title_from_filename(apk_path.name)

            release = get_or_create_release(repo_full_name, token, tag_name, release_title)
            upload_url = release.get("upload_url", "")
            if not upload_url:
                raise RuntimeError(f"Release upload_url missing for tag {tag_name}")

            download_url = upload_release_asset(upload_url, token, apk_path)
            entry = existing_by_url.get(download_url)
            if entry:
                entry["asset_name"] = apk_path.name
                entry["download_url"] = download_url
            else:
                entry = {
                    "name": release_title,
                    "version": "1.0",
                    "asset_name": apk_path.name,
                    "download_url": download_url,
                }
                apps.append(entry)
                existing_by_url[download_url] = entry
    finally:
        cleanup_tmp()

    update_readme(apps)
    save_state(apps)
    print(f"Processed {len(new_apks)} new APK file(s). Total APKs in store: {len(apps)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
