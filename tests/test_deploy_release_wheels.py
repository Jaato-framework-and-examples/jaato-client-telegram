"""deploy-vps.sh installs framework builds published only as GitHub Releases (issue #49).

The jaato repo is private and no longer publishes to (Test)PyPI: each build is a
GitHub Release (tag ``<pkg>-<version>``) carrying wheel + sdist + SHA256SUMS. The
script takes the pinned wheels from there (GITHUB_RELEASES=1) or from a directory
the operator filled (JAATO_WHEELS_DIR), verifies them, and hands them to uv as
--find-links -- to the framework install AND the bot's jaato-sdk pin, so the bot
stays on the framework's exact version. ``curl``, ``gh`` and ``uv`` are stubbed.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy-vps.sh"
REPO = "Jaato-framework-and-examples/jaato"
TOKEN = "ghp_secret_token_value"

# A fake GitHub API. The token must arrive in the -K config file, never in argv
# (argv is logged so the tests can assert that).
FAKE_CURL = """#!/usr/bin/env bash
echo "$*" >> "$CURL_LOG"
out=""; url=""; cfg=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -K) cfg="$2"; shift 2;;
    -H|--retry) shift 2;;
    -*) shift;;
    *) url="$1"; shift;;
  esac
done
grep -q "Bearer $EXPECT_TOKEN" "$cfg" || exit 22
case "$url" in
  */releases/tags/*) f="$FIXTURES/${url##*/}.json"; [ -f "$f" ] || exit 22; cat "$f";;
  */releases/assets/*) cp "$FIXTURES/assets/${url##*/}" "$out";;
  *) exit 22;;
esac
"""

FAKE_UV = """#!/usr/bin/env bash
echo "$*" >> "$UV_LOG"
[ "$1 $2" = "pip show" ] && echo "Version: $SDK"
exit 0
"""

FAKE_GH_FAILS = "#!/usr/bin/env bash\nexit 1\n"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / name).write_text(body)
    (bin_dir / name).chmod(0o755)


def _sums(files: dict) -> str:
    return "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items())


def _release(fixtures: Path, tag: str, files: dict, sums: "str | None" = None) -> None:
    """Publish a fake release: the wheels + an sdist (never downloaded) + SHA256SUMS."""
    assets = fixtures / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    sdist = {f"{tag}.tar.gz": b"sdist"}
    published = {**files, **sdist}
    if sums is None:
        sums = _sums(published)
    entries = []
    for name, data in {**published, "SHA256SUMS": sums.encode()}.items():
        (assets / f"{tag}__{name}").write_bytes(data)
        entries.append({"name": name,
                        "url": f"https://api.github.com/repos/{REPO}/releases/assets/{tag}__{name}"})
    (fixtures / f"{tag}.json").write_text(json.dumps({"tag_name": tag, "assets": entries}))


SDK_WHL = "jaato_sdk-0.30.0rc30-py3-none-any.whl"
SRV_WHL = "jaato_server-1.3.0rc32-py3-none-any.whl"


def _publish_both(fixtures: Path) -> None:
    _release(fixtures, "jaato-sdk-0.30.0rc30", {SDK_WHL: b"sdk wheel"})
    _release(fixtures, "jaato-server-1.3.0rc32", {SRV_WHL: b"server wheel"})


def _bash(snippet, tmp_path, **env):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "curl", FAKE_CURL)
    _stub(bin_dir, "uv", FAKE_UV)
    _stub(bin_dir, "gh", FAKE_GH_FAILS)
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "USER": "deployer",
        "CURL_LOG": str(tmp_path / "curl.log"), "UV_LOG": str(tmp_path / "uv.log"),
        "FIXTURES": str(tmp_path / "fixtures"), "EXPECT_TOKEN": TOKEN,
        "JAATO_SDK_VERSION": "0.30.0rc30", "JAATO_SERVER_VERSION": "1.3.0rc32",
        "PYTHON_BIN": "python3", **env,
    }
    return subprocess.run(["bash", "-c", f'source "{DEPLOY}"; {snippet}'],
                          env=full, capture_output=True, text=True)


def _links(tmp_path, **env):
    r = _bash('fw_index_args; printf "IDX:%s\\n" ${FW_IDX[@]+"${FW_IDX[@]}"}', tmp_path, **env)
    return r, [ln[4:] for ln in r.stdout.splitlines() if ln.startswith("IDX:") and ln[4:]]


def _cache(tmp_path, tag):
    return tmp_path / ".cache" / "jaato-tg" / "release-wheels" / tag


# ── GitHub Releases ──────────────────────────────────────────────────────────

def test_github_releases_download_verify_and_link_both_packages(tmp_path):
    _publish_both(tmp_path / "fixtures")
    r, idx = _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN)
    assert r.returncode == 0, r.stderr
    sdk_dir = _cache(tmp_path, "jaato-sdk-0.30.0rc30")
    srv_dir = _cache(tmp_path, "jaato-server-1.3.0rc32")
    assert idx == ["--find-links", str(sdk_dir), "--find-links", str(srv_dir)]
    assert (sdk_dir / SDK_WHL).read_bytes() == b"sdk wheel"
    assert (srv_dir / SRV_WHL).read_bytes() == b"server wheel"
    assert not list(sdk_dir.glob("*.tar.gz"))      # wheels only; the sdist is skipped
    # every dependency still resolves from PyPI: no index override, no --no-index
    assert "--no-index" not in idx and "--index-url" not in idx
    assert "--prerelease" not in idx               # explicit pins suffice


def test_the_token_never_appears_in_argv(tmp_path):
    _publish_both(tmp_path / "fixtures")
    r, _ = _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN)
    assert r.returncode == 0, r.stderr
    assert TOKEN not in (tmp_path / "curl.log").read_text()


def test_a_verified_cache_is_reused_without_downloading(tmp_path):
    _publish_both(tmp_path / "fixtures")
    assert _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN)[0].returncode == 0
    (tmp_path / "curl.log").unlink()
    r, _ = _links(tmp_path, GITHUB_RELEASES="1")   # no token needed either
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "curl.log").exists()


def test_a_tampered_wheel_is_refused(tmp_path):
    fixtures = tmp_path / "fixtures"
    _release(fixtures, "jaato-sdk-0.30.0rc30", {SDK_WHL: b"sdk wheel"},
             sums=_sums({SDK_WHL: b"what was built"}))
    _release(fixtures, "jaato-server-1.3.0rc32", {SRV_WHL: b"server wheel"})
    r, _ = _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN)
    assert r.returncode != 0
    assert "SHA256SUMS" in r.stderr


def test_a_wheel_missing_from_the_sums_is_refused(tmp_path):
    """--ignore-missing must not let an UNLISTED wheel through unchecked."""
    fixtures = tmp_path / "fixtures"
    _release(fixtures, "jaato-sdk-0.30.0rc30",
             {SDK_WHL: b"sdk wheel", "evil-1.0-py3-none-any.whl": b"x"},
             sums=_sums({SDK_WHL: b"sdk wheel"}))
    _release(fixtures, "jaato-server-1.3.0rc32", {SRV_WHL: b"server wheel"})
    r, _ = _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN)
    assert r.returncode != 0


def test_a_release_without_sums_is_refused(tmp_path):
    fixtures = tmp_path / "fixtures"
    _publish_both(fixtures)
    meta = fixtures / "jaato-sdk-0.30.0rc30.json"
    d = json.loads(meta.read_text())
    d["assets"] = [a for a in d["assets"] if a["name"] != "SHA256SUMS"]
    meta.write_text(json.dumps(d))
    r, _ = _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN)
    assert r.returncode != 0
    assert "no SHA256SUMS" in r.stderr


def test_a_missing_release_names_the_tag(tmp_path):
    (tmp_path / "fixtures").mkdir()
    r, _ = _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN)
    assert r.returncode != 0
    assert "jaato-sdk-0.30.0rc30" in r.stderr


def test_no_token_is_a_clear_error(tmp_path):
    _publish_both(tmp_path / "fixtures")
    r, _ = _links(tmp_path, GITHUB_RELEASES="1")   # gh stub is not logged in
    assert r.returncode != 0
    assert "JAATO_GITHUB_TOKEN" in r.stderr and "JAATO_WHEELS_DIR" in r.stderr


def test_the_token_falls_back_to_a_logged_in_gh(tmp_path):
    _publish_both(tmp_path / "fixtures")
    _stub(tmp_path / "bin", "curl", FAKE_CURL)
    _stub(tmp_path / "bin", "gh", f'#!/usr/bin/env bash\necho "{TOKEN}"\n')
    # _bash would re-stub gh as failing: run the snippet directly
    r = subprocess.run(
        ["bash", "-c", f'source "{DEPLOY}"; fw_index_args'],
        env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path),
             "CURL_LOG": str(tmp_path / "curl.log"), "FIXTURES": str(tmp_path / "fixtures"),
             "EXPECT_TOKEN": TOKEN, "GITHUB_RELEASES": "1", "PYTHON_BIN": "python3",
             "JAATO_SDK_VERSION": "0.30.0rc30", "JAATO_SERVER_VERSION": "1.3.0rc32"},
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── the rules both sources share ─────────────────────────────────────────────

def test_release_sources_need_both_versions_pinned(tmp_path):
    for unpinned in ("JAATO_SDK_VERSION", "JAATO_SERVER_VERSION"):
        r, _ = _links(tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN, **{unpinned: ""})
        assert r.returncode != 0
        assert "JAATO_SDK_VERSION and JAATO_SERVER_VERSION" in r.stderr


def test_release_sources_do_not_combine_with_testpypi(tmp_path):
    r, _ = _links(tmp_path, GITHUB_RELEASES="1", TESTPYPI="1")
    assert r.returncode != 0 and "alternative framework sources" in r.stderr


def test_pypi_default_is_unchanged(tmp_path):
    r, idx = _links(tmp_path, JAATO_SDK_VERSION="", JAATO_SERVER_VERSION="")
    assert r.returncode == 0, r.stderr
    assert idx == []


# ── a local wheel directory (no GitHub credentials on the box) ───────────────

def test_wheels_dir_is_linked_and_its_sums_checked(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / SDK_WHL).write_bytes(b"sdk wheel")
    (wheels / SRV_WHL).write_bytes(b"server wheel")
    r, idx = _links(tmp_path, JAATO_WHEELS_DIR=str(wheels))
    assert r.returncode == 0, r.stderr             # no SHA256SUMS: operator's own files
    assert idx == ["--find-links", str(wheels)]

    (wheels / "SHA256SUMS").write_text(_sums({SDK_WHL: b"sdk wheel", SRV_WHL: b"other"}))
    r, _ = _links(tmp_path, JAATO_WHEELS_DIR=str(wheels))
    assert r.returncode != 0 and "SHA256SUMS" in r.stderr


def test_wheels_dir_must_exist(tmp_path):
    r, _ = _links(tmp_path, JAATO_WHEELS_DIR=str(tmp_path / "nope"))
    assert r.returncode != 0 and "not a directory" in r.stderr


# ── the bot venv takes the framework's exact SDK from the same wheels ────────

def test_the_bot_sdk_pin_uses_the_release_wheels_and_the_bot_pypi_only(tmp_path):
    _publish_both(tmp_path / "fixtures")
    r = _bash(
        'PYV=/fw/bin/python; BOT_PYV=/bot/bin/python; BOT_DIR=/bot/src; VENV=/fw; '
        'BOT_USER=jaato-tg; own_bot_home(){ :; }; as_bot(){ echo "$SDK"; }; sync_bot_sdk',
        tmp_path, GITHUB_RELEASES="1", JAATO_GITHUB_TOKEN=TOKEN, SDK="0.30.0rc30",
    )
    assert r.returncode == 0, r.stderr
    installs = [c for c in (tmp_path / "uv.log").read_text().splitlines()
                if c.startswith("pip install")]
    pin, bot = installs
    assert pin.endswith("jaato-sdk==0.30.0rc30")
    assert f"--find-links {_cache(tmp_path, 'jaato-sdk-0.30.0rc30')}" in pin
    assert "-e /bot/src" in bot and "--find-links" not in bot
