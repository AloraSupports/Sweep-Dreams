#!/usr/bin/env bash
# One-time setup on a fresh Debian 12 Google Cloud VM. Copy this file over and run:
#   bash setup-vm.sh
# (Not "curl | bash": the script waits for you to add a deploy key, which needs a terminal.)
#
# It installs Docker, clones the repo with a read-only deploy key, creates deploy/.env
# from the example, and tells you what to fill in. It does not start the app until
# .env is filled in (run: cd ~/sweep/deploy && docker compose up -d --build).
set -euo pipefail

REPO_SSH="git@github.com:AloraSupports/Sweep-Dreams.git"
DIR="$HOME/sweep"

echo "== 1/5 Docker"
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq ca-certificates curl gnupg git
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER"
  echo "   Docker installed. (Log out and back in once so your user can run docker without sudo.)"
else
  echo "   already installed"
fi

echo "== 2/5 Deploy key (read-only access to the private repo)"
if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
  ssh-keygen -t ed25519 -N "" -C "sweep-vm deploy key" -f "$HOME/.ssh/id_ed25519" >/dev/null
fi
ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
echo "   Add this public key in GitHub: repo Settings -> Deploy keys -> Add (read-only):"
echo
cat "$HOME/.ssh/id_ed25519.pub"
echo
read -r -p "   Press Enter once the deploy key is added... " _ </dev/tty

echo "== 3/5 Clone"
if [ ! -d "$DIR/.git" ]; then
  git clone -q "$REPO_SSH" "$DIR"
else
  git -C "$DIR" pull -q
fi

echo "== 4/5 Environment file"
cd "$DIR/deploy"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "   Created deploy/.env (owner-only). Fill in every line with: nano $DIR/deploy/.env"
else
  echo "   deploy/.env already exists; leaving it alone"
fi

echo "== 5/5 Can this VM reach the portal and the state form?"
for host in evv-dashboard.mobilecaregiverplus.com forms.monday.com; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$host/" || true)
  echo "   $host -> HTTP ${code:-none}"
done
echo "   (Any 2xx/3xx/4xx means the host answers from this IP. 'none' or 000 means blocked; see the plan doc's fallback.)"

cat <<EOF

Next:
  1. nano $DIR/deploy/.env        (portal login, Google OAuth client, a random session secret)
  2. cd $DIR/deploy && docker compose up -d --build
  3. docker compose logs -f app   (Ctrl-C to stop watching)
  4. Copy the AxisCare reports in (the folder exists from the first start):
       docker compose cp caregivers.csv app:/data/axiscare/
       docker compose cp authorizations.csv app:/data/axiscare/
  5. Open https://\$APP_DOMAIN, sign in, run a sweep with "prepare at most 1".
EOF
