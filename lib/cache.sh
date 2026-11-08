# shellcheck shell=bash
# Synthesis result cache for the local engines. Sourced, not executed.
#
# A press of the same text through the same engine at the same voice and
# speed always produces the same audio, so the result is worth keeping: a
# repeated notification costs one file read instead of one synthesis. Only
# local, deterministic engines are keyed this way (kokoro, kokoro-onnx,
# piper); cloud providers are never cached here because their voices can
# change server-side.
#
# Blur-vs-die summary of the safety properties:
# - The cache directory is owner-only (0700) and its files 0600; a symlinked
#   entry is refused, never followed.
# - The key includes the engine, voice and speed, and hashes the exact bytes
#   the engine would receive, so a different sanitizer output can never be
#   served under another text's name.
# - Audio is staged under a dot-file while synthesizing and renamed into
#   place only after a successful play, so a half-synthesized press (it was
#   cancelled, or died mid-stream) is never cached.
# - Caching is best effort in every direction: a refusal, a full disk, or a
#   race with another press simply means the press synthesizes as always.
# - `speak --verify` sets TTS_CACHE=0 so health checks still exercise real
#   synthesis rather than round-tripping a stored file.
#
# Knobs: TTS_CACHE=0 disables; TTS_CACHE_LIMIT_MB (default 256) and
# TTS_CACHE_TTL_DAYS (default 30) bound the directory, oldest first.

audio_cache_enabled() {
  [[ "${TTS_CACHE:-1}" != "0" ]]
}

audio_cache_dir() {
  printf '%s\n' "${TTS_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}}/omarchy-tts/audio"
}

audio_cache_key() { # <provider> <voice> <rate> — text on stdin, key on stdout
  { printf '%s\0%s\0%s\0' "$1" "$2" "$3"; cat; } | sha256sum | cut -d' ' -f1
}

audio_cache_key_valid() { # <key>
  [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

audio_cache_prepare() { # create/adopt the cache directory, or fail quietly
  local dir
  dir="$(audio_cache_dir)" || return 1
  [[ ! -L "$dir" ]] || return 1          # never follow a symlinked cache
  if [[ -e "$dir" ]]; then
    [[ -d "$dir" && -w "$dir" && -O "$dir" ]] || return 1
  else
    mkdir -p -- "$dir" 2>/dev/null || return 1
  fi
  chmod 0700 -- "$dir" 2>/dev/null
  return 0
}

audio_cache_lookup() { # <provider> <key> — cached path on stdout on a hit
  audio_cache_enabled || return 1
  audio_cache_key_valid "$2" || return 1
  local dir file now mtime ttl
  dir="$(audio_cache_dir)" || return 1
  file="$dir/$1-$2.pcm"
  [[ -f "$file" && ! -L "$file" ]] || return 1
  now="$(date +%s)" || return 1
  mtime="$(stat -c %Y -- "$file")" || return 1
  ttl=$(( ${TTS_CACHE_TTL_DAYS:-30} * 86400 ))
  if (( now - mtime > ttl )); then
    rm -f -- "$file"
    return 1
  fi
  touch -- "$file" 2>/dev/null  # hit refreshes mtime: this is the LRU order
  chmod 0600 -- "$file" 2>/dev/null
  printf '%s\n' "$file"
}

audio_cache_stage() { # <provider> <key> — staging path, or nothing on refusal
  audio_cache_enabled || return 0
  audio_cache_key_valid "$2" || return 0
  audio_cache_prepare || return 0
  printf '%s/.inflight-%s.part\n' "$(audio_cache_dir)" "$2"
}

audio_cache_commit() { # <provider> <key> <stage> — publish only real audio
  audio_cache_enabled || { rm -f -- "$3" 2>/dev/null; return 1; }
  audio_cache_key_valid "$2" || { rm -f -- "$3" 2>/dev/null; return 1; }
  if [[ ! -s "$3" ]]; then
    rm -f -- "$3" 2>/dev/null
    return 1
  fi
  chmod 0600 -- "$3" 2>/dev/null || { rm -f -- "$3"; return 1; }
  mv -f -- "$3" "$(audio_cache_dir)/$1-$2.pcm" 2>/dev/null \
    || { rm -f -- "$3" 2>/dev/null; return 1; }
  audio_cache_trim
  return 0
}

audio_cache_trim() { # drop expired entries first, then oldest past the cap
  local dir cap_mb cap now ttl
  dir="$(audio_cache_dir)" || return 0
  [[ -d "$dir" && ! -L "$dir" ]] || return 0
  cap_mb="${TTS_CACHE_LIMIT_MB:-256}"
  ttl=$(( ${TTS_CACHE_TTL_DAYS:-30} * 86400 ))
  # Anything expired, plus stages an earlier press never finished.
  find "$dir" -maxdepth 1 -name '*.pcm' -type f \
    -mmin +"$(( ttl / 60 ))" -delete 2>/dev/null
  find "$dir" -maxdepth 1 -name '.inflight-*.part' -type f \
    -mmin +60 -delete 2>/dev/null
  cap=$(( cap_mb * 1024 * 1024 ))
  # Oldest first (T@ ascends with mtime, and hits refresh mtime), delete
  # until the directory fits the budget. NUL-delimited: paths cannot carry
  # surprises, and sort -z keeps the record aligned.
  find "$dir" -maxdepth 1 -name '*.pcm' -type f -printf '%T@\t%s\t%p\0' 2>/dev/null \
    | LC_ALL=C sort -zn \
    | awk -v cap="$cap" 'BEGIN { RS = "\0"; FS = "\t" }
        { size[NR] = $2; path[NR] = $3; total += $2 }
        END { i = 1
              while (total > cap && i <= NR) { printf "%s\0", path[i]; total -= size[i]; i++ } }' \
    | xargs -0 -r rm -f -- 2>/dev/null
  return 0
}
