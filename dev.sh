#!/usr/bin/env bash
# BlamixFiles developer workflow. Works in Git Bash on Windows, and on macOS/Linux.
#
#   ./dev.sh setup              create .venv and install everything (app + tests + build)
#   ./dev.sh run                start the app from source
#   ./dev.sh cli ls mysite:/    run the command-line tool (any blamixfiles arguments)
#   ./dev.sh test [-k name]     run the tests (extra arguments go to pytest)
#   ./dev.sh check              tests + lint + app selftest: run before you push
#   ./dev.sh ship               check, then pull --rebase and push (no merge commits)
#   ./dev.sh build              portable app in dist/BlamixFiles (+ a .zip / .tar.gz)
#   ./dev.sh msi [--test]       Windows installer from dist/BlamixFiles (needs .NET SDK 8+)
#   ./dev.sh release v0.1.0     check, bump version, CHANGELOG, commit, tag, push
#                               (--dry-run to preview, nothing is changed)
#   ./dev.sh clean              remove build output and caches (keeps .venv)
set -euo pipefail
cd "$(dirname "$0")"

# ------------------------------------------------------------------ environment
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) WIN=1 ;;
  *) WIN=0 ;;
esac
if [ "$WIN" = 1 ]; then
  VPY=.venv/Scripts/python.exe
  SEP=";"                                   # PyInstaller --add-data separator
  # Qt's offscreen platform (tests, selftest) doesn't find Windows fonts on its own
  export QT_QPA_FONTDIR="${QT_QPA_FONTDIR:-${WINDIR:-C:/Windows}/Fonts}"
else
  VPY=.venv/bin/python
  SEP=":"
fi

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✖\033[0m %s\n' "$*" >&2; exit 1; }

find_python() {
  # Windows: the py launcher (the "python3" in Git Bash is often the Microsoft Store stub)
  local c
  for c in ${PYTHON:-} "py -3" python3 python; do
    [ -z "$c" ] && continue
    if $c -c "import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

ensure_venv() {   # $1 = requirements file
  local req="${1:-requirements.txt}"
  if [ ! -x "$VPY" ] && [ ! -f "$VPY" ]; then
    local py
    py=$(find_python) || die "Python 3.10+ not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')."
    say "Creating .venv with $($py --version 2>&1)"
    $py -m venv .venv
    "$VPY" -m pip install -q --upgrade pip
  fi
  local stamp=".venv/.installed-$(basename "$req" .txt)"
  # the dev requirements include the app's, so they count for requirements.txt too
  if [ "$req" = requirements.txt ] && [ -f .venv/.installed-requirements-dev ] && \
     [ ! requirements.txt -nt .venv/.installed-requirements-dev ]; then
    return
  fi
  if [ ! -f "$stamp" ] || [ "$req" -nt "$stamp" ] || [ requirements.txt -nt "$stamp" ]; then
    say "Installing $req"
    "$VPY" -m pip install -q -r "$req"
    touch "$stamp"
  fi
}

# ------------------------------------------------------------------ commands
cmd_setup() {
  ensure_venv requirements-dev.txt
  "$VPY" -m pip install -q pyinstaller ruff
  ok "Ready. Try: ./dev.sh run   or   ./dev.sh test"
}

cmd_run() {
  ensure_venv requirements.txt
  exec "$VPY" run.py "$@"
}

cmd_cli() {
  ensure_venv requirements.txt
  exec "$VPY" -m blamixfiles.cli "$@"
}

cmd_test() {
  ensure_venv requirements-dev.txt
  QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen} "$VPY" -m pytest -q tests "$@"
}

cmd_check() {
  ensure_venv requirements-dev.txt
  "$VPY" -m ruff --version >/dev/null 2>&1 || "$VPY" -m pip install -q ruff
  say "Lint"
  "$VPY" -m ruff check --select F,E9,B --ignore B008,B905,B009 blamixfiles tests
  say "Tests"
  QT_QPA_PLATFORM=offscreen "$VPY" -m pytest -q tests
  say "App selftest"
  local out
  out=$(QT_QPA_PLATFORM=offscreen BLAMIXFILES_SELFTEST=1 "$VPY" run.py 2>&1) || true
  if ! grep -q "SELFTEST OK" <<<"$out"; then
    echo "$out" | tail -25
    die "The app didn't start (output above)"
  fi
  ok "App starts"
  ok "All checks passed"
}

cmd_ship() {
  [ -z "$(git status --porcelain)" ] || die "Commit (or stash) your changes first."
  if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    say "Pull (rebase)"
    git pull --rebase --quiet
    [ -n "$(git log --oneline '@{u}..')" ] || { ok "Nothing to push."; return; }
    cmd_check
    git push
  else                                      # first push of this branch
    cmd_check
    git push -u origin HEAD
  fi
  ok "Pushed. CI: https://github.com/blamixology/blamixfiles/actions"
}

cmd_build() {
  ensure_venv requirements.txt
  "$VPY" -m pip show pyinstaller >/dev/null 2>&1 || "$VPY" -m pip install -q pyinstaller
  local os_name arch app exe data icon=() extra=()
  case "$WIN$(uname -s)" in
    1*) os_name=windows ;; 0Darwin) os_name=macos ;; *) os_name=linux ;;
  esac
  case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; *) arch=x64 ;; esac
  if [ "$os_name" = macos ]; then
    app=dist/BlamixFiles.app; exe=$app/Contents/MacOS/BlamixFiles; data=dist/data
    "$VPY" -m pip show pillow >/dev/null 2>&1 || "$VPY" -m pip install -q pillow   # png -> icns
    icon=(--icon blamixfiles/assets/app.png)
    extra=(--osx-bundle-identifier ro.blamixology.blamixfiles)
  else
    app=dist/BlamixFiles; exe=$app/BlamixFiles; data=$app/data
    [ "$WIN" = 1 ] && { exe=$exe.exe; icon=(--icon blamixfiles/assets/app.ico); }
  fi
  if [ -d "$data" ]; then                     # keep your data when rebuilding over an old build
    say "Keeping $data"
    rm -rf _data_backup && mv "$data" _data_backup
  fi
  say "PyInstaller ($os_name-$arch)"
  "$VPY" -m PyInstaller --noconfirm --clean --onedir --windowed --name BlamixFiles \
    ${icon[@]+"${icon[@]}"} ${extra[@]+"${extra[@]}"} \
    --add-data "blamixfiles/assets${SEP}blamixfiles/assets" \
    --collect-submodules pygments.lexers --collect-submodules pygments.styles \
    --exclude-module tkinter --exclude-module PySide6.QtWebEngineCore \
    --exclude-module PySide6.QtWebEngineWidgets --exclude-module PySide6.Qt3DCore \
    --exclude-module PySide6.QtCharts --exclude-module PySide6.QtMultimedia \
    --exclude-module PySide6.QtQuick3D --exclude-module PySide6.QtDesigner \
    --log-level WARN run.py
  [ "$os_name" = macos ] && rm -rf dist/BlamixFiles        # the .app is what you want
  "$VPY" packaging/prune_qt.py "$app"
  rm -rf build BlamixFiles.spec

  say "Selftest of the built app"
  # (the windowed exe has no console on Windows, so only its exit code counts)
  if QT_QPA_PLATFORM=offscreen BLAMIXFILES_SELFTEST=1 "$exe" >/dev/null 2>&1; then
    ok "Built app starts"
  else
    [ -d _data_backup ] && mv _data_backup "$data"
    die "The built app failed its selftest"
  fi

  say "Archive"
  local out="dist/BlamixFiles-$os_name-$arch"
  rm -f "$out.zip" "$out.tar.gz"
  case "$os_name" in
    macos) (cd dist && ditto -c -k --keepParent BlamixFiles.app "BlamixFiles-$os_name-$arch.zip") ;;
    windows) "$VPY" -c "import shutil; shutil.make_archive('$out', 'zip', 'dist', 'BlamixFiles')" ;;
    *) tar -C dist -czf "$out.tar.gz" BlamixFiles ;;
  esac
  [ -d _data_backup ] && mv _data_backup "$data"          # after archiving: your data never ships
  ls -1 dist/BlamixFiles-* 2>/dev/null
  ok "Done: $app"
  if [ "$os_name" = macos ]; then
    echo "   Unsigned app: open it the first time with right-click → Open."
  fi
}

cmd_msi() {
  [ "$WIN" = 1 ] || die "The MSI installer is built on Windows."
  [ -f dist/BlamixFiles/BlamixFiles.exe ] || cmd_build
  local args=()
  [ "${1:-}" = "--test" ] && args=(-Test)
  powershell -NoProfile -ExecutionPolicy Bypass -File packaging/windows/build_msi.ps1 ${args[@]+"${args[@]}"}
}

cmd_release() {
  local dry=0 tag=""
  for a in "$@"; do
    case "$a" in --dry-run) dry=1 ;; *) tag="$a" ;; esac
  done
  [[ "$tag" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]] || die "usage: ./dev.sh release [--dry-run] vX.Y.Z"
  local version="${BASH_REMATCH[1]}"
  [ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "Switch to main first."
  [ -z "$(git status --porcelain)" ] || die "Commit (or stash) your changes first."
  say "Syncing with GitHub"
  git fetch --tags --quiet origin
  git pull --rebase --quiet origin main
  git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "Tag $tag already exists."

  local last range notes
  last=$(git tag --list 'v*' --sort=-v:refname | head -1)
  range=${last:+$last..}HEAD
  notes=$(git log --no-merges --pretty='- %s (%h)' "$range" | grep -v -i -E '^- (release v|update changelog)' || true)
  [ -n "$notes" ] || die "Nothing new since ${last:-the start}."

  local entry
  entry=$("$VPY" - "$version" "$notes" <<'PY'
import datetime, re, sys
version, notes = sys.argv[1], sys.argv[2]
text = open("CHANGELOG.md", encoding="utf-8").read()
m = re.search(r"^## Unreleased\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
intro = m.group(1).strip() if m else ""
body = (intro + "\n\n" if intro else "") + notes
entry = f"## {version} - {datetime.date.today().isoformat()}\n\n{body}\n"
if m:
    text = text[:m.start()] + "## Unreleased\n\n" + entry + "\n" + text[m.end():]
else:
    head, sep, rest = text.partition("\n\n")
    text = head + sep + "## Unreleased\n\n" + entry + "\n" + rest
open("CHANGELOG.new", "w", encoding="utf-8", newline="\n").write(text)
print(entry)
PY
)
  echo; echo "$entry"
  trap 'rm -f CHANGELOG.new' EXIT
  if [ "$dry" = 1 ]; then
    rm -f CHANGELOG.new
    ok "Dry run: nothing changed. Run without --dry-run to release $tag."
    return
  fi
  cmd_check
  mv CHANGELOG.new CHANGELOG.md
  sed -i.bak -E "s/^__version__ = \".*\"/__version__ = \"$version\"/" blamixfiles/__init__.py
  rm -f blamixfiles/__init__.py.bak
  git add CHANGELOG.md blamixfiles/__init__.py
  git commit --quiet -m "Release $tag"
  git tag "$tag"
  say "Pushing main + $tag"
  git push --atomic origin main "$tag" || die "Push failed; the tag is still local. Fix it, then: git push --atomic origin main $tag"
  trap - EXIT
  ok "Released $tag"
}

cmd_clean() {
  rm -rf build dist BlamixFiles.spec .pytest_cache .ruff_cache CHANGELOG.new
  find blamixfiles tests -name __pycache__ -type d -prune -exec rm -rf {} +
  ok "Clean"
}

cmd_help() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

cmd="${1:-help}"
shift || true
case "$cmd" in
  setup|run|cli|test|check|ship|build|msi|release|clean|help) "cmd_$cmd" "$@" ;;
  -h|--help) cmd_help ;;
  *) die "Unknown command '$cmd'. Try: ./dev.sh help" ;;
esac
