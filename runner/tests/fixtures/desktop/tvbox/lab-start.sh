#!/bin/sh
# lab-start.sh: dark's start script for the TV box image in a lab sandbox
# (rave 1112; dark specs/desktop-arm). Runs as root inside the container,
# before dark's executor. Brings up the TV box's own session the way the NUC
# runs it (mango with the image's config, noctalia, user tv) on a headless
# output of the TV's size, 3840x2160 at scale 3, and writes the session's
# environment to /run/dark-desktop-session for the executor and the checks.
#
# Taken from rave evidence/1094/suite/run.sh, which proved it on images
# ab17 to ab42. The user is switched with setpriv, not runuser: the image
# gives tv rtprio 95 in limits.d, which pam_limits cannot apply in an
# unprivileged container.
set -u
useradd -m -u 1001 tv 2>/dev/null
install -d -o tv -g tv -m 700 /tmp/xdg
cp /usr/share/tvbox/mango/config.conf /tmp/mango.conf
printf '\nmonitorrule=name:^HEADLESS-1$,width:3840,height:2160,refresh:60,x:0,y:0,scale:3\n' >> /tmp/mango.conf
chmod 644 /tmp/mango.conf
cat > /tmp/session.sh <<'S'
export XDG_RUNTIME_DIR=/tmp/xdg WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=gles2 XDG_CURRENT_DESKTOP=mango
cd ~
systemd-socket-activate -l /tmp/xdg/bus dbus-broker-launch --scope user >/tmp/xdg/bus.log 2>&1 &
export DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/xdg/bus
sleep 0.5
mango -c /tmp/mango.conf >/tmp/xdg/mango.log 2>&1 &
# Both sockets: the IPC one can appear before wayland-0, and a noctalia
# started in between dies with "failed to connect to Wayland display".
for _ in $(seq 1 75); do ls /tmp/xdg/mango-*.sock >/dev/null 2>&1 && [ -S /tmp/xdg/wayland-0 ] && break; sleep 0.2; done
sleep 1
export MANGO_INSTANCE_SIGNATURE=$(ls /tmp/xdg/mango-*.sock | head -1) WAYLAND_DISPLAY=wayland-0
noctalia >/tmp/xdg/noctalia.log 2>&1 &
printf 'DARK_USER=tv\nDARK_COMPOSITOR=mango\nXDG_RUNTIME_DIR=/tmp/xdg\nWAYLAND_DISPLAY=wayland-0\nDBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/xdg/bus\nMANGO_INSTANCE_SIGNATURE=%s\nHOME=/var/lib/tv\n' "$MANGO_INSTANCE_SIGNATURE" > /tmp/xdg/dark-desktop-session
wait
S
chmod 755 /tmp/session.sh
HOME=/var/lib/tv USER=tv setpriv --reuid tv --regid tv --init-groups sh /tmp/session.sh >/dev/null 2>&1 &
# ready when the env file exists and noctalia has drawn its bar (its log says so)
for _ in $(seq 1 150); do
    [ -s /tmp/xdg/dark-desktop-session ] && grep -q '\[bar\] creating' /tmp/xdg/noctalia.log 2>/dev/null && break
    sleep 0.2
done
[ -s /tmp/xdg/dark-desktop-session ] || { echo "lab-start: no session after 30 s" >&2; cat /tmp/xdg/mango.log >&2; exit 1; }
cp /tmp/xdg/dark-desktop-session /run/dark-desktop-session
echo "lab-start: mango and noctalia up for tv"
