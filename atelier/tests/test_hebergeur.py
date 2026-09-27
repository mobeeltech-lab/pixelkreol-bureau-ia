"""Test du service hôte pk-hebergeur avec un vrai nginx (certbot et systemctl remplacés par des doublures).
Prérequis : PK Atelier lancé (tests/test_e2e.py) sur le port 8000, données dans /tmp/atdata, nginx installé, root."""
import json, os, shutil, subprocess, time, glob
import httpx

ok = 0
def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1; print("✓", msg)

ROOT, DATA = "/tmp/pkroot", "/tmp/atdata"
H = f"{DATA}/hebergement"
FAKE = "/tmp/pkfake"
LE = "/tmp/pkle/live"
shutil.rmtree(ROOT, ignore_errors=True); shutil.rmtree(FAKE, ignore_errors=True); shutil.rmtree("/tmp/pkle", ignore_errors=True)
os.makedirs(ROOT); os.makedirs(FAKE); os.makedirs(LE)
os.symlink(DATA, f"{ROOT}/data")
open(f"{ROOT}/.env", "w").write("ATELIER_IP_VPS=127.0.0.1\nATELIER_HOTE_DEMO=demo.localhost\n")
# doublures : systemctl reload nginx → nginx -s reload ; certbot → crée le dossier du certificat (échec pour *.echec.re)
open(f"{FAKE}/systemctl", "w").write('#!/bin/sh\n[ "$1" = reload ] && exec nginx -s reload\nexit 0\n')
open(f"{FAKE}/certbot", "w").write(f'''#!/bin/sh
echo "$@" >> {FAKE}/certbot.log
if [ "$1" = delete ]; then rm -rf {LE}/"$3"; exit 0; fi
for a in "$@"; do case "$a" in *echec.re) echo "Challenge failed" >&2; exit 1;; esac; done
while [ $# -gt 0 ]; do [ "$1" = --cert-name ] && mkdir -p {LE}/"$2"; shift; done
exit 0
''')
for f in ("systemctl", "certbot"):
    os.chmod(f"{FAKE}/{f}", 0o755)
# /etc/hosts : domaines de test
hosts = open("/etc/hosts").read()
ajout = "\n".join(f"127.0.0.1 {d}" for d in ["vitrine-test.demo.localhost", "client-ok.re", "www.client-ok.re", "echec.re", "agents.test"])
if "client-ok.re" not in hosts:
    open("/etc/hosts", "a").write("\n" + ajout + "\n")
# un service existant à protéger
os.makedirs("/etc/nginx/sites-enabled", exist_ok=True)
open("/etc/nginx/sites-available/agents", "w").write("server { listen 80; server_name agents.test; return 200 'agents'; }\n")
if not os.path.exists("/etc/nginx/sites-enabled/agents"):
    os.symlink("/etc/nginx/sites-available/agents", "/etc/nginx/sites-enabled/agents")
for f in glob.glob("/etc/nginx/sites-*/pk-site-*"):
    os.remove(f)
subprocess.run(["nginx", "-s", "stop"], capture_output=True); time.sleep(0.5)
subprocess.run(["nginx"], check=True)

env = {**os.environ, "PK_DIR": ROOT, "PK_PORT": "8000", "PK_LE_LIVE": LE, "PATH": f"{FAKE}:{os.environ['PATH']}"}
def worker(*args):
    r = subprocess.run(["python3", "hote/pk-hebergeur.py", *args], env=env, capture_output=True, text=True)
    return r.stdout + r.stderr
def demande(d, action="ajouter", type_="demo", brut=None):
    p = f"{H}/demandes/{d}.json"
    open(p, "w").write(brut if brut is not None else json.dumps({"domaine": d, "action": action, "type": type_, "site": "x"}))
    os.chown(p, 10001, 10001)
def etat(d):
    return json.load(open(f"{H}/etat/{d}.json"))

for f in glob.glob(f"{H}/demandes/*") + glob.glob(f"{H}/attente/*") + glob.glob(f"{H}/etat/*"):
    os.remove(f)

# site réel créé via l'API (sous-domaine vitrine-test)
K = {"Authorization": "Bearer cle-test"}
c = httpx.Client(base_url="http://127.0.0.1:8000", timeout=60)
s = c.post("/api/sites/generer", headers=K, json={"entreprise": "Vitrine Test", "activite": "Test", "sous_domaine": "vitrine-test"}).json()
check(s["slug"] == "vitrine-test" and os.path.exists(f"{H}/demandes/vitrine-test.demo.localhost.json"), "PK Atelier dépose la demande")

# attaques et demandes invalides
demande("casse", brut="{pas du json")
os.symlink("/etc/passwd", f"{H}/demandes/lien.json")
demande("evil.com", type_="demo")                     # un « démo » hors du domaine démo
demande("agents.test", type_="client")                # déjà servi par un autre service
open("/tmp/victime", "w").write("intact")
os.symlink("/tmp/victime", f"{H}/etat/client-ok.re.json")   # piège : lien vers un fichier système
demande("client-ok.re", type_="client")
demande("nulle-part-pk-test.re", type_="client")      # DNS absent
demande("echec.re", type_="client")                    # certificat refusé
out = worker()
check(not os.path.exists(f"{H}/demandes/casse.json") and not os.path.lexists(f"{H}/demandes/lien.json"), "demande illisible et lien symbolique écartés")
check(open("/etc/passwd").read().startswith("root"), "aucun fichier système touché")
check(etat("evil.com")["etat"] == "refuse", "démo hors de demo.<domaine> refusée")
check(etat("agents.test")["etat"] == "refuse" and not os.path.exists("/etc/nginx/sites-available/pk-site-agents.test"), "domaine d'un service existant protégé")

e = etat("vitrine-test.demo.localhost")
check(e["etat"] == "actif" and e["https"] and os.path.exists("/etc/nginx/sites-enabled/pk-site-vitrine-test.demo.localhost"), "sous-domaine démo branché + certificat")
check(subprocess.run(["nginx", "-t"], capture_output=True).returncode == 0, "configuration nginx valide")
time.sleep(0.5)
via = httpx.get("http://127.0.0.1/", headers={"Host": "vitrine-test.demo.localhost"})
check(via.status_code == 200 and "Vitrine Test" in via.text, "le site répond à travers nginx sur son sous-domaine")
check(httpx.get("http://127.0.0.1/api/sites", headers={"Host": "vitrine-test.demo.localhost", **K}).status_code == 404, "API jamais exposée via un site hébergé")
check(httpx.get("http://127.0.0.1/", headers={"Host": "agents.test"}).text == "agents", "service existant intact")

check(open("/tmp/victime").read() == "intact" and not os.path.islink(f"{H}/etat/client-ok.re.json"), "piège du lien symbolique déjoué (fichier d'état remplacé, cible intacte)")
ec = etat("client-ok.re")
check(ec["etat"] == "actif" and ec["noms"] == ["client-ok.re", "www.client-ok.re"], "domaine client branché avec www")
check(os.stat(f"{H}/etat/client-ok.re.json").st_uid == 10001, "état lisible par le conteneur")
en = etat("nulle-part-pk-test.re")
check(en["etat"] == "dns_en_attente" and os.path.exists(f"{H}/attente/nulle-part-pk-test.re.json") and not os.listdir(f"{H}/demandes"), "DNS absent → file d'attente, dossier des demandes vidé (pas de boucle systemd)")
ee = etat("echec.re")
check(ee["etat"] == "http_seulement" and ee["essais_cert"] == 1 and os.path.exists(f"{H}/attente/echec.re.json"), "certificat refusé → HTTP seulement, nouvel essai plus tard")
for _ in range(5):
    worker("--tout")
ee = etat("echec.re")
check(ee["essais_cert"] == 5 and not os.path.exists(f"{H}/attente/echec.re.json"), "essais Let's Encrypt plafonnés (quotas)")
old = time.time() - 8 * 86400
os.utime(f"{H}/attente/nulle-part-pk-test.re.json", (old, old))
worker("--tout")
check(etat("nulle-part-pk-test.re")["etat"] == "abandon", "abandon après 7 jours sans DNS")

# état vu par PK Atelier
eh = c.get("/api/sites/vitrine-test/hebergement", headers=K).json()
check(eh["sous_domaine_https"] and eh["url"] == "https://vitrine-test.demo.localhost/", "PK Atelier lit l'état : le lien HTTPS devient le lien à donner")

# retrait via l'API (site démo) → le service hôte nettoie
c.delete("/api/sites/vitrine-test", headers=K)
worker()
check(not os.path.exists("/etc/nginx/sites-available/pk-site-vitrine-test.demo.localhost") and not os.path.exists(f"{LE}/vitrine-test.demo.localhost")
      and not os.path.exists(f"{H}/etat/vitrine-test.demo.localhost.json"), "retrait : configuration, certificat et état supprimés")
check(subprocess.run(["nginx", "-t"], capture_output=True).returncode == 0, "nginx toujours valide après retrait")
subprocess.run(["nginx", "-s", "stop"], capture_output=True)
print(f"\n{ok} contrôles réussis (service hôte)")
