#!/usr/bin/env python3
"""
Non-interactive Readerware 4.31 modernizer for Ubuntu Linux.

Performs:
  1. Installs latest Azul Zulu OpenJDK 8 (GA, x64, glibc) via Azul Metadata API
     and wires it into Readerware's JRE path.
  2. Downloads HSQLDB 2.3.4 (bridge), HSQLDB 2.7.4-jdk8 (verifies Java 8 bytecode),
     and Jython 2.7.4 standalone (GitHub Releases primary + SHA-256 verification).
  3. Backs up and step-migrates discovered HSQLDB databases:
       1.8.x (SET SCRIPTFORMAT TEXT; SHUTDOWN SCRIPT)
         -> 2.3.4 (SHUTDOWN SCRIPT)
         -> 2.7.4-jdk8 (SHUTDOWN COMPACT)
  4. Replaces hsqldb.jar and jython.jar (preserving original .bak files).
  5. Tunes JVM flags (-Xmx2048m, system AA fonts, OpenGL).
  6. Disables Readerware auto-update and scraper updates in rwUser4.properties.
  7. Installs the LOC FOLIO SRU MARCXML scraper (rwloc.py) and updates both
     rwSite4.properties files in ISO-8859-1 encoding.
"""

import hashlib
import json
import os
import pwd
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (easy to expose via argparse later)
# ---------------------------------------------------------------------------
REAL_USER = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
PW_ENTRY = pwd.getpwnam(REAL_USER)
REAL_HOME = Path(PW_ENTRY.pw_dir)
REAL_UID = PW_ENTRY.pw_uid
REAL_GID = PW_ENTRY.pw_gid

CONFIG = {
    "rw_install_dir": Path("/opt/readerware4"),
    "rw_user_dir": REAL_HOME / ".readerware3",
    "zulu_install_dir": Path("/opt/zulu8"),
    "backup_root": REAL_HOME / "readerware_modernize_backup",
    # Directories scanned recursively for HSQLDB *.properties database files
    "db_search_dirs": [
        REAL_HOME / ".readerware3",
        REAL_HOME / "Documents",
        REAL_HOME / "Readerware",
    ],
    "hsql_user": "SA",
    "hsql_pass": "",
    "jvm_heap_max": "2048m",
    "jvm_extra_flags": [
        "-Dawt.useSystemAAFontSettings=on",
        "-Dsun.java2d.opengl=true",
    ],
    "azul_api_url": (
        "https://api.azul.com/metadata/v1/zulu/packages/"
        "?java_version=8&os=linux&arch=x64&archive_type=tar.gz"
        "&java_package_type=jdk&javafx_bundled=false&latest=true"
        "&release_status=ga&availability_types=CA"
    ),
    "hsqldb_234_urls": [
        "https://repo1.maven.org/maven2/org/hsqldb/hsqldb/2.3.4/hsqldb-2.3.4.jar",
    ],
    "hsqldb_274_urls": [
        "https://repo1.maven.org/maven2/org/hsqldb/hsqldb/2.7.4/hsqldb-2.7.4-jdk8.jar",
    ],
    "jython_274_urls": [
        "https://github.com/jython/jython/releases/download/v2.7.4/jython-standalone-2.7.4.jar",
        "https://repo1.maven.org/maven2/org/python/jython-standalone/2.7.4/jython-standalone-2.7.4.jar",
    ],
    "jython_274_sha256": "1fba1769effcc8b19f5e10436bc8274a158ce988559f257927c24c73bb137f3c",
}

# Slot 1 = ISBN, Slot 2 = LCCN, Slot 3 = LCCN (change Slot 3 to bath.title=%22{0}%22 if using Title Auto-Update)
LOC_SITE_UNESCAPED = (
    "Library of Congress;100;U;N;"
    "https://lx2.loc.gov/sru/lcdb?version=1.1&operation=searchRetrieve&query=bath.isbn={0}&maximumRecords=1&recordSchema=marcxml;"
    "https://lx2.loc.gov/sru/lcdb?version=1.1&operation=searchRetrieve&query=bath.lccn={0}&maximumRecords=1&recordSchema=marcxml;"
    "https://lx2.loc.gov/sru/lcdb?version=1.1&operation=searchRetrieve&query=bath.lccn={0}&maximumRecords=1&recordSchema=marcxml"
    ";;;;;;rwloc.py;rwloc.py;rwloc.py;UTF-8;true"
)

LOC_SITE_ESCAPED = (
    "Library of Congress;100;U;N;"
    r"https\://lx2.loc.gov/sru/lcdb?version\=1.1&operation\=searchRetrieve&query\=bath.isbn\={0}&maximumRecords\=1&recordSchema\=marcxml;"
    r"https\://lx2.loc.gov/sru/lcdb?version\=1.1&operation\=searchRetrieve&query\=bath.lccn\={0}&maximumRecords\=1&recordSchema\=marcxml;"
    r"https\://lx2.loc.gov/sru/lcdb?version\=1.1&operation\=searchRetrieve&query\=bath.lccn\={0}&maximumRecords\=1&recordSchema\=marcxml"
    ";;;;;;rwloc.py;rwloc.py;rwloc.py;UTF-8;true"
)

HSQL_MIGRATOR_JAVA = r"""
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;

public class HsqlMigrator {
    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: HsqlMigrator <jdbc_url> <sql1> [sql2 ...]");
            System.exit(1);
        }
        try {
            Class.forName("org.hsqldb.jdbc.JDBCDriver");
        } catch (ClassNotFoundException e) {
            Class.forName("org.hsqldb.jdbcDriver");
        }
        String url = args[0];
        String user = System.getProperty("hsql.user", "SA");
        String pass = System.getProperty("hsql.pass", "");
        Connection conn = DriverManager.getConnection(url, user, pass);
        try {
            Statement st = conn.createStatement();
            try {
                for (int i = 1; i < args.length; i++) {
                    st.execute(args[i]);
                }
            } finally {
                try { st.close(); } catch (Exception ignored) {}
            }
        } finally {
            try { conn.close(); } catch (Exception ignored) {}
        }
    }
}
""".lstrip()

RWLOC_PY = r'''# -*- coding: utf-8 -*-
# Library of Congress FOLIO SRU (MARCXML) scraper for Readerware 4.31 + Jython 2.7.4

import os
import re
import xml.etree.ElementTree as ET

import rw
from rwsupport import appendComment


def local_name(tag):
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def clean_punct(text, chars=" /:;.,"):
    if not text:
        return ""
    return text.strip().rstrip(chars).strip()


def clean_author_name(name):
    if not name:
        return ""
    name = name.strip()
    if name.endswith(","):
        name = name[:-1].strip()
    if name.endswith(".") and (len(name) < 3 or name[-3] != " "):
        name = name[:-1].strip()
    return name


def get_subfields(datafield, codes=None):
    results = []
    for child in datafield:
        if local_name(child.tag) == "subfield":
            code = child.attrib.get("code", "")
            if codes is None or code in codes:
                if child.text:
                    results.append((code, child.text.strip()))
    return results


def first_subfield(datafield, code):
    for child in datafield:
        if local_name(child.tag) == "subfield" and child.attrib.get("code") == code:
            if child.text:
                return child.text.strip()
    return ""


def extract_year(date_str):
    if not date_str:
        return ""
    m = re.search(r"(\d{4})", date_str)
    return m.group(1) if m else ""


def extract_marcxml(source):
    rw.setDefaultPublicationPlace("US")
    rw.setDefaultLanguage("English")
    rw.setCurrencySymbol("$")

    if not source:
        return

    if isinstance(source, unicode):
        xml_bytes = source.encode("utf-8")
    else:
        xml_bytes = source

    xml_bytes = xml_bytes.strip()
    if not xml_bytes.startswith("<"):
        return

    try:
        root = ET.fromstring(xml_bytes)
    except Exception, e:
        print "rwloc.py: XML parse error:", e
        return

    for elem in root.iter():
        if local_name(elem.tag) == "numberOfRecords" and elem.text:
            try:
                count = int(elem.text.strip())
                rw.setResultCount(count)
                if count == 0:
                    return
            except Exception:
                pass
            break

    # Locate the inner MARC21 <record> (skip the outer <zs:record> wrapper)
    record = None
    for elem in root.iter():
        if local_name(elem.tag) == "record":
            for child in elem:
                if local_name(child.tag) == "datafield":
                    record = elem
                    break
            if record is not None:
                break

    if record is None:
        return

    fields = {}
    for child in record:
        if local_name(child.tag) == "datafield":
            tag = child.attrib.get("tag", "")
            fields.setdefault(tag, []).append(child)

    # Authors (100 main personal name, 700 added personal names)
    authorList = []
    seen_authors = set()
    for tag in ("100", "700"):
        for df in fields.get(tag, []):
            raw = first_subfield(df, "a")
            name = clean_author_name(raw)
            if name:
                converted = rw.convertName(name)
                if converted and converted not in seen_authors:
                    seen_authors.add(converted)
                    authorList.append(converted)
    if authorList:
        rw.setAuthors(authorList)

    # Title (245 $a + $b)
    if "245" in fields:
        df = fields["245"][0]
        title_a = first_subfield(df, "a")
        title_b = first_subfield(df, "b")
        if title_b:
            title = clean_punct(title_a, " /") + " " + clean_punct(title_b, " /.")
        else:
            title = clean_punct(title_a, " /:.")
        if title:
            rw.setTitle(title)

    # Publication Place, Publisher, Date (264 ind2=1 preferred, else 264, else 260)
    pub_df = None
    for df in fields.get("264", []):
        if df.attrib.get("ind2") == "1":
            pub_df = df
            break
    if pub_df is None and "264" in fields:
        pub_df = fields["264"][0]
    if pub_df is None and "260" in fields:
        pub_df = fields["260"][0]

    if pub_df is not None:
        place = clean_punct(first_subfield(pub_df, "a"), " :;,[]")
        if place:
            rw.setPublicationPlace(place)

        publisher = clean_punct(first_subfield(pub_df, "b"), " ,;.")
        if publisher:
            rw.setPublisher(publisher)

        raw_date = first_subfield(pub_df, "c")
        year = extract_year(raw_date)
        if year:
            rw.setReleaseDate(year)
            if raw_date.lower().startswith("c") or u"\u00a9" in raw_date:
                rw.setCopyrightDate(year)

    for df in fields.get("264", []):
        if df.attrib.get("ind2") == "4":
            c_year = extract_year(first_subfield(df, "c"))
            if c_year:
                rw.setCopyrightDate(c_year)
                break

    # Pages & Dimensions (300 $a, $c)
    if "300" in fields:
        df = fields["300"][0]
        extent = first_subfield(df, "a")
        if extent:
            m = re.search(r"(\d+)\s*(?:p\.|pages)", extent, re.IGNORECASE)
            if m:
                rw.setPages(m.group(1))

        dim = clean_punct(first_subfield(df, "c"), " .;")
        if dim:
            rw.setDimensions(dim)

    # ISBN & Format (020 $a, $q)
    chosen_isbn = ""
    chosen_fmt = ""
    for df in fields.get("020", []):
        raw_a = first_subfield(df, "a")
        raw_q = first_subfield(df, "q")
        if not raw_a:
            continue

        isbn_token = raw_a.split()[0].replace("-", "").strip()
        qual_text = (raw_a + " " + raw_q).lower()

        if "mass market" in qual_text:
            fmt = "Mass Market Paperback"
        elif "pbk" in qual_text or "paper" in qual_text or "softcover" in qual_text:
            fmt = "Paperback"
        else:
            fmt = "Hardcover"

        if not chosen_isbn or (len(isbn_token) == 13 and len(chosen_isbn) != 13):
            chosen_isbn = isbn_token
            chosen_fmt = fmt

    if chosen_isbn:
        rw.setISBN(chosen_isbn)
    if chosen_fmt:
        rw.setFormat(chosen_fmt)

    # LCCN (010 $a)
    if "010" in fields:
        lccn = first_subfield(fields["010"][0], "a").strip()
        if lccn:
            rw.setLCCN(lccn)

    # LC Call Number (050 $a + $b)
    if "050" in fields:
        df = fields["050"][0]
        parts = [v for _, v in get_subfields(df, ("a", "b"))]
        if parts:
            rw.setCallNumber(" ".join(parts))

    # Dewey Decimal (082 $a)
    if "082" in fields:
        dewey = first_subfield(fields["082"][0], "a").replace("/", "").strip()
        if dewey:
            rw.setDewey(dewey)

    # Series (490 preferred, fallback 830)
    for tag in ("490", "830"):
        if tag in fields:
            df = fields[tag][0]
            parts = [clean_punct(v, " ;.") for _, v in get_subfields(df, ("a", "v"))]
            series = " ".join([p for p in parts if p])
            if series:
                rw.setSeries(series)
                break

    # Comments / Product Info (520 Summary, 505 Contents, 500 Notes)
    comments = ""
    for tag in ("520", "505", "500"):
        for df in fields.get(tag, []):
            text = first_subfield(df, "a")
            if text:
                comments = appendComment(comments, text)
    if comments:
        rw.setProductInfo(comments)

    # Subjects / Keywords (650, 651, 600, 610)
    keywords = []
    seen_kw = set()
    for tag in ("650", "651", "600", "610"):
        for df in fields.get(tag, []):
            parts = [clean_punct(v, " .") for _, v in get_subfields(df, ("a", "x", "y", "z", "v"))]
            kw = " -- ".join([p for p in parts if p])
            if kw and kw not in seen_kw:
                seen_kw.add(kw)
                keywords.append(kw)
    if keywords:
        rw.setKeywords(", ".join(keywords))

    # Categories (655 Form/Genre preferred, fallback to first 3 subjects)
    cats = []
    for df in fields.get("655", []):
        cat = clean_punct(first_subfield(df, "a"), " .")
        if cat and cat not in cats:
            cats.append(cat)
    if not cats:
        cats = keywords[:3]

    if len(cats) >= 1:
        rw.setCategory1(cats[0])
    if len(cats) >= 2:
        rw.setCategory2(cats[1])
    if len(cats) >= 3:
        rw.setCategory3(cats[2])


try:
    rw.setBookObject(Book)
    extract_marcxml(source)
finally:
    if os.path.exists(rw.getDocsDir() + "rwuserexit.py"):
        execfile(rw.getDocsDir() + "rwuserexit.py") in globals()
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[modernize] {msg}", flush=True)


def chown_user(path: Path) -> None:
    if os.geteuid() != 0:
        return
    if path.is_dir():
        for root, dirs, files in os.walk(path):
            os.chown(root, REAL_UID, REAL_GID)
            for name in dirs + files:
                os.chown(Path(root) / name, REAL_UID, REAL_GID)
    elif path.exists():
        os.chown(path, REAL_UID, REAL_GID)


def ensure_not_running() -> None:
    proc = subprocess.run(
        ["pgrep", "-f", "readerware"],
        capture_output=True,
        text=True,
    )
    pids = [p for p in proc.stdout.strip().splitlines() if p and int(p) != os.getpid()]
    if pids:
        raise RuntimeError(
            f"Readerware appears to be running (PIDs: {', '.join(pids)}). "
            "Close Readerware before running this script."
        )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(urls: list[str], dest: Path, expected_sha256: str | None = None) -> Path:
    last_err = None
    for url in urls:
        log(f"Downloading {url} -> {dest.name}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "readerware-modernizer/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
                shutil.copyfileobj(resp, out)
            if expected_sha256:
                actual = sha256_file(dest)
                if actual.lower() != expected_sha256.lower():
                    raise ValueError(f"SHA-256 mismatch: expected {expected_sha256}, got {actual}")
            return dest
        except Exception as exc:
            last_err = exc
            log(f"  Download failed ({exc}); trying fallback if available...")
            if dest.exists():
                dest.unlink()
    raise RuntimeError(f"Failed to download {dest.name}: {last_err}")


def verify_java8_bytecode(jar_path: Path, class_entry: str) -> None:
    with zipfile.ZipFile(jar_path, "r") as zf:
        data = zf.read(class_entry)
    magic, _minor, major = struct.unpack(">IHH", data[:8])
    if magic != 0xCAFEBABE:
        raise RuntimeError(f"{jar_path.name}:{class_entry} is not a valid Java class file.")
    if major > 52:
        raise RuntimeError(
            f"{jar_path.name} targets bytecode major version {major} (> 52 / Java 8)."
        )
    log(f"Verified {jar_path.name} bytecode major version = {major} (Java 8 compatible).")


def backup_once(src: Path, suffix: str = ".bak") -> Path:
    bak = src.with_name(src.name + suffix)
    if src.exists() and not bak.exists():
        shutil.copy2(src, bak)
        log(f"Backed up {src} -> {bak}")
    return bak


# ---------------------------------------------------------------------------
# Step 1: Azul Zulu OpenJDK 8
# ---------------------------------------------------------------------------
def install_zulu_java8(workdir: Path) -> Path:
    log("Querying Azul Metadata API for latest Zulu OpenJDK 8 (Linux x64)...")
    req = urllib.request.Request(
        CONFIG["azul_api_url"], headers={"User-Agent": "readerware-modernizer/1.0"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        packages = json.loads(resp.read().decode("utf-8"))

    if not packages:
        raise RuntimeError("Azul Metadata API returned no matching Zulu 8 packages.")

    pkg = packages[0]
    tarball_path = workdir / pkg["name"]
    download_file([pkg["download_url"]], tarball_path, pkg.get("sha256_hash"))

    extract_dir = workdir / "zulu_extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball_path, "r:gz") as tf:
        tf.extractall(extract_dir)

    extracted_roots = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(extracted_roots) != 1:
        raise RuntimeError(f"Unexpected Zulu archive layout in {extract_dir}")

    zulu_dir = CONFIG["zulu_install_dir"]
    if zulu_dir.exists() or zulu_dir.is_symlink():
        shutil.rmtree(zulu_dir) if zulu_dir.is_dir() and not zulu_dir.is_symlink() else zulu_dir.unlink()
    shutil.move(str(extracted_roots[0]), str(zulu_dir))
    log(f"Installed Azul Zulu 8 at {zulu_dir}")

    # Point Readerware's bundled JRE path at Zulu 8's JRE
    rw_jre = CONFIG["rw_install_dir"] / "jre"
    zulu_jre = zulu_dir / "jre" if (zulu_dir / "jre").is_dir() else zulu_dir
    if rw_jre.exists() or rw_jre.is_symlink():
        rw_jre_bak = CONFIG["rw_install_dir"] / "jre.bak"
        if not rw_jre_bak.exists() and not rw_jre.is_symlink():
            shutil.move(str(rw_jre), str(rw_jre_bak))
            log(f"Backed up bundled JRE -> {rw_jre_bak}")
        else:
            shutil.rmtree(rw_jre) if rw_jre.is_dir() and not rw_jre.is_symlink() else rw_jre.unlink()
    rw_jre.symlink_to(zulu_jre)
    log(f"Symlinked {rw_jre} -> {zulu_jre}")

    return zulu_dir


# ---------------------------------------------------------------------------
# Step 2 & 3: Stepped HSQLDB Migration (1.8.x -> 2.3.4 -> 2.7.4-jdk8)
# ---------------------------------------------------------------------------
def discover_hsqldb_databases(search_dirs: list[Path]) -> list[Path]:
    """Return unique HSQLDB database prefixes (path without .properties)."""
    prefixes: list[Path] = []
    seen: set[Path] = set()
    for base in search_dirs:
        if not base.exists():
            continue
        for prop_file in base.rglob("*.properties"):
            # Skip Readerware's own config properties and backup folders
            if CONFIG["backup_root"] in prop_file.parents:
                continue
            try:
                head = prop_file.read_text(encoding="iso-8859-1", errors="ignore")[:512]
            except OSError:
                continue
            if "HSQL Database Engine" in head or re.search(r"(?m)^hsqldb\.", head):
                prefix = prop_file.with_suffix("")
                resolved = prefix.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    prefixes.append(prefix)
    return prefixes


def read_hsql_version(prefix: Path) -> str:
    text = prefix.with_suffix(".properties").read_text(encoding="iso-8859-1", errors="ignore")
    m = re.search(r"(?m)^version\s*=\s*([^\s#]+)", text)
    return m.group(1).strip() if m else "unknown"


def compile_migrator(zulu_dir: Path, workdir: Path) -> Path:
    java_src = workdir / "HsqlMigrator.java"
    java_src.write_text(HSQL_MIGRATOR_JAVA, encoding="utf-8")
    javac = zulu_dir / "bin" / "javac"
    subprocess.run([str(javac), str(java_src)], check=True)
    return workdir


def run_hsql_step(
    java_bin: Path, helper_dir: Path, hsql_jar: Path, db_prefix: Path, sql_statements: list[str]
) -> None:
    jdbc_url = f"jdbc:hsqldb:file:{db_prefix};ifexists=true"
    cmd = [
        str(java_bin),
        f"-Dhsql.user={CONFIG['hsql_user']}",
        f"-Dhsql.pass={CONFIG['hsql_pass']}",
        "-cp",
        f"{helper_dir}:{hsql_jar}",
        "HsqlMigrator",
        jdbc_url,
        *sql_statements,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"HSQLDB migration failed on {db_prefix} with {hsql_jar.name}:\n"
            f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )


def migrate_databases(
    zulu_dir: Path,
    workdir: Path,
    jar_18: Path,
    jar_234: Path,
    jar_274: Path,
    backup_dir: Path,
) -> None:
    dbs = discover_hsqldb_databases(CONFIG["db_search_dirs"])
    if not dbs:
        log("No HSQLDB databases discovered in configured search dirs; skipping DB migration.")
        return

    helper_dir = compile_migrator(zulu_dir, workdir)
    java_bin = zulu_dir / "bin" / "java"
    db_backup_dir = backup_dir / "databases"
    db_backup_dir.mkdir(parents=True, exist_ok=True)

    for prefix in dbs:
        lck = prefix.with_suffix(".lck")
        if lck.exists():
            raise RuntimeError(f"Lock file exists for {prefix} ({lck}). Refusing to migrate.")

        version = read_hsql_version(prefix)
        log(f"Found database: {prefix} (HSQLDB version={version})")
        if version.startswith("2.7."):
            log("  Already on HSQLDB 2.7.x; skipping.")
            continue

        # Back up all sibling files matching "<prefix.name>.*"
        target_bak = db_backup_dir / prefix.name
        target_bak.mkdir(parents=True, exist_ok=True)
        for sibling in prefix.parent.glob(f"{prefix.name}.*"):
            if sibling.is_file():
                shutil.copy2(sibling, target_bak / sibling.name)
        log(f"  Backed up database files -> {target_bak}")

        if version.startswith("1.8"):
            log("  Step 1/3 (1.8.x): SET SCRIPTFORMAT TEXT + SHUTDOWN SCRIPT")
            run_hsql_step(java_bin, helper_dir, jar_18, prefix, ["SET SCRIPTFORMAT TEXT", "SHUTDOWN SCRIPT"])

        if version.startswith("1.8") or version.startswith("2.0") or version.startswith("2.1") or version.startswith("2.2") or version.startswith("2.3"):
            log("  Step 2/3 (2.3.4 bridge): SHUTDOWN SCRIPT")
            run_hsql_step(java_bin, helper_dir, jar_234, prefix, ["SHUTDOWN SCRIPT"])

        log("  Step 3/3 (2.7.4-jdk8): SHUTDOWN COMPACT")
        run_hsql_step(java_bin, helper_dir, jar_274, prefix, ["SHUTDOWN COMPACT"])

        # Restore original user ownership on rewritten DB files
        for sibling in prefix.parent.glob(f"{prefix.name}.*"):
            chown_user(sibling)

        log(f"  Migrated {prefix.name} -> version={read_hsql_version(prefix)}")


# ---------------------------------------------------------------------------
# Step 4: Install HSQLDB 2.7.4-jdk8 and Jython 2.7.4
# ---------------------------------------------------------------------------
def install_jars(jar_274: Path, jython_jar: Path) -> None:
    lib_dir = CONFIG["rw_install_dir"] / "lib"

    target_hsql = lib_dir / "hsqldb.jar"
    backup_once(target_hsql)
    shutil.copy2(jar_274, target_hsql)
    log(f"Installed {jar_274.name} -> {target_hsql}")

    target_jython = lib_dir / "jython.jar"
    backup_once(target_jython)
    shutil.copy2(jython_jar, target_jython)
    log(f"Installed {jython_jar.name} -> {target_jython}")


# ---------------------------------------------------------------------------
# Step 5: Tune JVM Launch Parameters
# ---------------------------------------------------------------------------
def patch_jvm_flags() -> None:
    rw_dir = CONFIG["rw_install_dir"]
    heap_flag = f"-Xmx{CONFIG['jvm_heap_max']}"
    desired_flags = [heap_flag, *CONFIG["jvm_extra_flags"]]

    vmoptions_files = list(rw_dir.glob("*.vmoptions"))
    if vmoptions_files:
        for vmf in vmoptions_files:
            backup_once(vmf)
            lines = vmf.read_text(encoding="utf-8", errors="ignore").splitlines()
            lines = [ln for ln in lines if not ln.strip().startswith("-Xmx")]
            for flag in desired_flags:
                if flag not in [ln.strip() for ln in lines]:
                    lines.append(flag)
            vmf.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log(f"Updated JVM flags in {vmf}")
        return

    # Fallback: patch shell launcher script(s) invoking readerware.jar
    patched_any = False
    for script in rw_dir.iterdir():
        if not script.is_file() or script.suffix in (".jar", ".bak", ".png", ".ico", ".txt"):
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "readerware" not in text.lower() or "java" not in text.lower():
            continue
        if not text.startswith("#!"):
            continue

        backup_once(script)
        new_text = re.sub(r"-Xmx\S+", heap_flag, text)
        missing = [f for f in desired_flags if f not in new_text]
        if missing:
            flag_str = " ".join(missing)
            # Inject right after the java invocation or -Xmx flag
            if heap_flag in new_text:
                new_text = new_text.replace(heap_flag, f"{heap_flag} {flag_str}", 1)
            else:
                new_text = re.sub(r"(\bjava\b)", rf"\1 {flag_str}", new_text, count=1)

        if new_text != text:
            script.write_text(new_text, encoding="utf-8")
            log(f"Updated JVM flags in launcher script {script}")
            patched_any = True

    if not patched_any:
        log("WARNING: No .vmoptions or shell launcher found in /opt/readerware4 to patch JVM flags.")


# ---------------------------------------------------------------------------
# Step 6: Disable Auto-Updates (rwUser4.properties)
# ---------------------------------------------------------------------------
def configure_user_properties(backup_dir: Path) -> None:
    user_props = CONFIG["rw_user_dir"] / "rwUser4.properties"
    if not user_props.exists():
        log(f"WARNING: {user_props} not found; skipping auto-update flags.")
        return

    shutil.copy2(user_props, backup_dir / "rwUser4.properties")
    lines = user_props.read_text(encoding="iso-8859-1").splitlines()

    updates = {
        "rw.auto.update": "false",
        "rw.scraper.update": "false",
    }
    found = set()
    new_lines = []
    for line in lines:
        replaced = False
        for key, val in updates.items():
            if re.match(rf"^{re.escape(key)}\s*=", line):
                new_lines.append(f"{key}={val}")
                found.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    for key, val in updates.items():
        if key not in found:
            new_lines.append(f"{key}={val}")

    user_props.write_text("\n".join(new_lines) + "\n", encoding="iso-8859-1")
    chown_user(user_props)
    log(f"Disabled auto-updates in {user_props}")


# ---------------------------------------------------------------------------
# Step 7: LOC FOLIO SRU MARCXML Scraper & rwSite4.properties
# ---------------------------------------------------------------------------
def update_site_properties(props_path: Path, loc_value: str, backup_dest: Path) -> None:
    if not props_path.exists():
        log(f"WARNING: {props_path} not found; skipping.")
        return

    backup_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(props_path, backup_dest)

    lines = props_path.read_text(encoding="iso-8859-1").splitlines()
    loc_pattern = re.compile(r"^(rw\.site\.url(\d+))=Library of Congress;")
    url_pattern = re.compile(r"^rw\.site\.url(\d+)=")

    max_idx = -1
    replaced = False
    new_lines = []

    for line in lines:
        m_url = url_pattern.match(line)
        if m_url:
            max_idx = max(max_idx, int(m_url.group(1)))

        m_loc = loc_pattern.match(line)
        if m_loc:
            key = m_loc.group(1)
            new_lines.append(f"{key}={loc_value}")
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        next_key = f"rw.site.url{max_idx + 1}"
        new_lines.append(f"{next_key}={loc_value}")
        log(f"Added {next_key}=Library of Congress to {props_path}")
    else:
        log(f"Updated existing Library of Congress entry in {props_path}")

    props_path.write_text("\n".join(new_lines) + "\n", encoding="iso-8859-1")
    chown_user(props_path)


def install_loc_scraper(backup_dir: Path) -> None:
    scrapers_dir = CONFIG["rw_user_dir"] / "scrapers4"
    scrapers_dir.mkdir(parents=True, exist_ok=True)

    rwloc_path = scrapers_dir / "rwloc.py"
    if rwloc_path.exists():
        (backup_dir / "scrapers4").mkdir(parents=True, exist_ok=True)
        shutil.copy2(rwloc_path, backup_dir / "scrapers4" / "rwloc.py")

    rwloc_path.write_text(RWLOC_PY, encoding="utf-8")
    chown_user(rwloc_path)
    log(f"Installed LOC FOLIO SRU MARCXML scraper -> {rwloc_path}")

    update_site_properties(
        scrapers_dir / "rwSite4.properties",
        LOC_SITE_UNESCAPED,
        backup_dir / "scrapers4" / "rwSite4.properties",
    )
    update_site_properties(
        CONFIG["rw_user_dir"] / "rwSite4.properties",
        LOC_SITE_ESCAPED,
        backup_dir / "rwSite4.properties",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if os.geteuid() != 0:
        print("Error: Run this script with sudo so it can update /opt/readerware4 and /opt/zulu8.", file=sys.stderr)
        return 1

    if not CONFIG["rw_install_dir"].is_dir():
        print(f"Error: Readerware install directory not found: {CONFIG['rw_install_dir']}", file=sys.stderr)
        return 1

    ensure_not_running()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = CONFIG["backup_root"] / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    log(f"Target user: {REAL_USER} ({REAL_HOME})")
    log(f"Backup directory: {backup_dir}")

    with tempfile.TemporaryDirectory(prefix="rw_modernize_") as tmp:
        workdir = Path(tmp)

        # 1. Install Azul Zulu Java 8
        zulu_dir = install_zulu_java8(workdir)

        # 2. Download HSQLDB 2.3.4, HSQLDB 2.7.4-jdk8, and Jython 2.7.4
        jar_234 = download_file(CONFIG["hsqldb_234_urls"], workdir / "hsqldb-2.3.4.jar")
        jar_274 = download_file(CONFIG["hsqldb_274_urls"], workdir / "hsqldb-2.7.4-jdk8.jar")
        verify_java8_bytecode(jar_274, "org/hsqldb/jdbc/JDBCDriver.class")

        jython_jar = download_file(
            CONFIG["jython_274_urls"],
            workdir / "jython-standalone-2.7.4.jar",
            CONFIG["jython_274_sha256"],
        )

        # 3. Preserve original 1.8.x hsqldb.jar and step-migrate databases
        orig_hsql_jar = CONFIG["rw_install_dir"] / "lib" / "hsqldb.jar"
        bak_hsql_jar = backup_once(orig_hsql_jar)
        jar_18 = bak_hsql_jar if bak_hsql_jar.exists() else orig_hsql_jar
        migrate_databases(zulu_dir, workdir, jar_18, jar_234, jar_274, backup_dir)

        # 4. Install new JARs
        install_jars(jar_274, jython_jar)

        # 5. Tune JVM launch parameters
        patch_jvm_flags()

        # 6. Lock down auto-updates
        configure_user_properties(backup_dir)

        # 7. Install LOC MARCXML scraper & update site configs
        install_loc_scraper(backup_dir)

    chown_user(CONFIG["backup_root"])
    log("All steps completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
