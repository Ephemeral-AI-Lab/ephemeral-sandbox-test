#!/bin/sh
# Render a captioned, narrated film from already-verified FlashCart frames.
# It deliberately never fabricates live agent activity or modifies evidence.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
FRAMES="$ROOT/ephemeral-sandbox-test/.e2e-state/flashcart/phase5/rehearsals/screenshots"
OUT="$ROOT/ephemeral-sandbox-docs/multiagent/film"
WORK="$OUT/.flashcart-film-work"
FONT="/System/Library/Fonts/Supplemental/Arial.ttf"

mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

for command in ffmpeg ffprobe say; do command -v "$command" >/dev/null; done
for source in "$FRAMES/live-desktop.png" "$FRAMES/recorded-mobile.png"; do test -f "$source"; done

escape_drawtext() {
  # drawtext uses ':' to separate filter options, including inside quoted text.
  printf '%s' "$1" | sed 's/:/\\:/g'
}

if [ "${1-}" = "--check" ]; then
  caption=$(escape_drawtext 'A08 rejected: source_conflict · no partial publish')
  test "$caption" = 'A08 rejected\: source_conflict · no partial publish'
  ffmpeg -v error -f lavfi -i "color=c=0xf8f5ef:s=1920x1080:d=0.1" \
    -vf "drawtext=fontfile=${FONT}:text='$caption':fontcolor=white:fontsize=31:x=94:y=955" \
    -frames:v 1 -f null -
  exit 0
fi

voice() {
  say -v Samantha -r 175 -o "$WORK/$1.aiff" "$2"
  ffmpeg -v error -y -i "$WORK/$1.aiff" -ar 48000 "$WORK/$1.m4a"
}

duration() {
  ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$1"
}

render_card() {
  name=$1
  title=$(escape_drawtext "$2")
  detail=$(escape_drawtext "$3")
  seconds=$(duration "$WORK/$name.m4a")
  ffmpeg -v error -y -f lavfi -i "color=c=0xf8f5ef:s=1920x1080:r=30" -i "$WORK/$name.m4a" \
    -filter_complex "[0:v]drawbox=x=0:y=0:w=1920:h=18:color=0x19626b:t=fill,drawtext=fontfile=${FONT}:text='$title':fontcolor=0x1f2926:fontsize=76:x=120:y=392,drawtext=fontfile=${FONT}:text='$detail':fontcolor=0x49605a:fontsize=34:x=124:y=510[v]" \
    -map "[v]" -map 1:a -t "$seconds" -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart "$WORK/$name.mp4"
}

render_desktop() {
  name=$1
  crop_y=$2
  heading=$(escape_drawtext "$3")
  detail=$(escape_drawtext "$4")
  seconds=$(duration "$WORK/$name.m4a")
  ffmpeg -v error -y -loop 1 -framerate 30 -i "$FRAMES/live-desktop.png" -i "$WORK/$name.m4a" \
    -filter_complex "[0:v]scale=1920:-2,crop=1920:1080:0:$crop_y,drawbox=x=0:y=830:w=1920:h=250:color=black@0.74:t=fill,drawtext=fontfile=${FONT}:text='$heading':fontcolor=white:fontsize=52:x=92:y=875,drawtext=fontfile=${FONT}:text='$detail':fontcolor=0xd9ede8:fontsize=31:x=94:y=955[v]" \
    -map "[v]" -map 1:a -t "$seconds" -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart "$WORK/$name.mp4"
}

render_mobile() {
  name=$1
  seconds=$(duration "$WORK/$name.m4a")
  ffmpeg -v error -y -loop 1 -framerate 30 -i "$FRAMES/recorded-mobile.png" -i "$WORK/$name.m4a" \
    -filter_complex "[0:v]scale=720:-2,crop=720:1080:0:0,pad=1920:1080:600:0:color=0xf8f5ef,drawbox=x=0:y=830:w=1920:h=250:color=0x1f2926@0.92:t=fill,drawtext=fontfile=${FONT}:text='Responsive recorded proof':fontcolor=white:fontsize=52:x=92:y=875,drawtext=fontfile=${FONT}:text='The completed offline storefront is retained after clean cleanup.':fontcolor=0xd9ede8:fontsize=31:x=94:y=955[v]" \
    -map "[v]" -map 1:a -t "$seconds" -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart "$WORK/$name.mp4"
}

voice intro "This is FlashCart, a verified recorded execution. Ten agents, one outcome."
voice lanes "Ten automatic workspaces were active at the same time. Each lane works sequentially, while all ten lanes progress in parallel. The runner completed 482 real public CLI calls."
voice merge "All ten primary lanes published. Agent labels remain a runner join to redacted raw owners, so presentation does not replace provenance."
voice conflict "The conflict wave proves atomicity. A06 changes the seeded line. A08 publishes from an older head and is rejected with source conflict. There is no revision advance and no partial publish. A fresh-head retry succeeds."
voice outcome "Session isolation is measured, not assumed. Two Shared sessions collide on port 4173. Two isolated sessions bind it. The completed offline storefront remains after clean cleanup."
voice close "The recorded package has 141 verified files: ten owners, one deliberate rejection, two isolated servers, and clean cleanup. FlashCart is complete."

render_card intro "FlashCart" "Ten agents · one verified outcome"
render_desktop lanes 0 "Ten gated workspaces" "10 concurrent lanes · 482 real public CLI calls"
render_desktop merge 360 "Merge and provenance" "10 owners retained · labels are runner joins"
render_desktop conflict 360 "Atomic conflict handling" "A08 rejected: source_conflict · no partial publish"
render_desktop outcome 1050 "Port isolation and retained preview" "2 isolated servers · preview preserved after clean cleanup"
render_card close "Verified outcome" "482 calls · 10 owners · 141 verified files · clean cleanup"

mkdir -p "$OUT"
ffmpeg -v error -y \
  -i "$WORK/intro.mp4" -i "$WORK/lanes.mp4" -i "$WORK/merge.mp4" -i "$WORK/conflict.mp4" -i "$WORK/outcome.mp4" -i "$WORK/close.mp4" \
  -filter_complex "[0:v][0:a][1:v][1:a][2:v][2:a][3:v][3:a][4:v][4:a][5:v][5:a]concat=n=6:v=1:a=1[v][a]" \
  -map "[v]" -map "[a]" -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart "$OUT/flashcart-ten-agent-evidence.mp4"

ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT/flashcart-ten-agent-evidence.mp4"
