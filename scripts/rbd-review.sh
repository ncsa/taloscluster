#!/usr/bin/env bash
# Review the ceph-csi RBD images in a pool, one at a time, and delete the ones
# you confirm.
#
# Run on a Ceph node (or anywhere `rbd` and `rados` have an admin keyring). For
# every image it prints the size, the space actually used, which PVC/PV it was
# provisioned for, when it was created and -- when the client updated them --
# when it was last accessed and written, then asks `Delete? [y/N]`. Enter keeps
# the image. A confirmed delete removes the image and both ceph-csi journal
# records (the per-volume omap object and its key in the pool directory), so
# nothing is left behind for a future PVC to collide with.
#
# An image with a watcher is mapped on some node right now and is never
# offered for deletion. Images with snapshots fail to delete; remove the
# snapshots first.
#
# Usage:
#   scripts/rbd-review.sh [POOL]        # default pool: kubernetes
#   scripts/rbd-review.sh POOL --all    # include images not created by ceph-csi
set -euo pipefail

pool=kubernetes
all=false
for arg in "$@"; do
  case $arg in
    --all) all=true ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) pool=$arg ;;
  esac
done

[ -t 0 ] || { echo "rbd-review: needs a terminal to ask for confirmation" >&2; exit 1; }
for tool in rbd rados; do
  command -v "$tool" >/dev/null || { echo "rbd-review: $tool not found" >&2; exit 1; }
done

# `rbd info` prints `<tab>key: value` lines; pick one value by key.
info_field() { awk -v k="$2" -F': ' '$1 ~ "^[[:space:]]*"k"$" {sub(/^[^:]*: /, ""); print; exit}' <<<"$1"; }

# The raw value of one omap key, or nothing when the object or key is absent.
omap_value() { rados -p "$pool" getomapval "$1" "$2" /dev/stdout 2>/dev/null || true; }

kept=0 deleted=0 skipped=0
while IFS= read -r image; do
  [ -n "$image" ] || continue
  uuid=${image#csi-vol-}
  if [ "$uuid" = "$image" ] && [ "$all" = false ]; then
    skipped=$((skipped + 1))
    continue
  fi

  info=$(rbd info "$pool/$image")
  size=$(awk '$1 == "size" {print $2, $3; exit}' <<<"$info")  # `size 1 GiB in 256 objects`, no colon
  created=$(info_field "$info" create_timestamp)
  accessed=$(info_field "$info" access_timestamp)
  modified=$(info_field "$info" modify_timestamp)
  used=$(rbd du "$pool/$image" 2>/dev/null | awk -v n="$image" '$1 == n {print $(NF-1), $NF}')
  watchers=$(rbd status "$pool/$image" 2>/dev/null | grep -c 'watcher=' || true)
  snaps=$(rbd snap ls "$pool/$image" 2>/dev/null | awk 'NR > 1' | wc -l | tr -d ' ')

  # provenance: image metadata when the provisioner recorded it, else the
  # ceph-csi journal, else nothing (not a ceph-csi image)
  meta=$(rbd image-meta list "$pool/$image" 2>/dev/null || true)
  pvc_ns=$(awk '$1 == "csi.storage.k8s.io/pvc/namespace" {print $2}' <<<"$meta")
  pvc=$(awk '$1 == "csi.storage.k8s.io/pvc/name" {print $2}' <<<"$meta")
  pv=$(awk '$1 == "csi.storage.k8s.io/pv/name" {print $2}' <<<"$meta")
  if [ -z "$pv" ] && [ "$uuid" != "$image" ]; then
    pv=$(omap_value "csi.volume.$uuid" csi.volname)
  fi

  echo
  echo "== $pool/$image"
  echo "   size:      ${size:-?}   used: ${used:-?}"
  echo "   created:   ${created:-unknown}"
  echo "   accessed:  ${accessed:-not recorded}"
  echo "   modified:  ${modified:-not recorded}"
  if [ -n "$pvc" ]; then
    echo "   claim:     $pvc_ns/$pvc   pv: $pv"
  elif [ -n "$pv" ]; then
    echo "   claim:     (pvc name not recorded)   pv: $pv"
  else
    echo "   claim:     none recorded (not provisioned by ceph-csi?)"
  fi
  [ "$snaps" = 0 ] || echo "   snapshots: $snaps (rbd rm will refuse until they are removed)"
  if [ "${watchers:-0}" != 0 ]; then
    echo "   IN USE:    $watchers watcher(s), mapped on a node; not offered for deletion"
    kept=$((kept + 1))
    continue
  fi

  # stdin is the image list; the answer must come from the terminal
  read -r -p "   Delete? [y/N] " answer </dev/tty || answer=  # EOF keeps the image
  case $answer in
    y|Y|yes|YES)
      rbd rm "$pool/$image"
      if [ "$uuid" != "$image" ]; then
        rados -p "$pool" rm "csi.volume.$uuid" 2>/dev/null || true
        [ -z "$pv" ] || rados -p "$pool" rmomapkey csi.volumes.default "csi.volume.$pv" 2>/dev/null || true
      fi
      echo "   deleted"
      deleted=$((deleted + 1))
      ;;
    *)
      echo "   kept"
      kept=$((kept + 1))
      ;;
  esac
done < <(rbd ls "$pool")

echo
echo "$pool: $deleted deleted, $kept kept, $skipped non-csi image(s) skipped (use --all to review them)"
