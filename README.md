# Server Deploy

Codex va Claude Code uchun Git loyihalarni umumiy VPS’ga deploy qiluvchi skill. `SERVER` va `PAROL` bilan boshlang‘ich ulanishni tekshiradi, `ssh-keygen` orqali loyiha uchun Ed25519 kalit yaratadi, keyingi ishlarni cheklangan `sdp-<loyiha>` foydalanuvchisi orqali bajaradi.

Repo manzili o‘zgarmagan: **xamidjonovk/safe-vps-deploy**. Skill/plugin nomi 2.0 versiyadan boshlab **server-deploy**.

## O‘rnatish

Windows PowerShell — Codex va Claude Code uchun:

```powershell
irm https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.ps1 | iex
```

Linux / macOS / WSL:

```bash
curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash
```

Faqat bitta agent uchun o‘rnatish:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.ps1 -OutFile install-server-deploy.ps1
.\install-server-deploy.ps1 -Agent codex
# Yoki: -Agent claude
```

```bash
curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash -s -- --codex
# Yoki: --claude
```

O‘rnatish joylari: Codex uchun `~/.codex/skills/server-deploy`, Claude Code uchun `~/.claude/skills/server-deploy`. Yangilash uchun o‘sha buyruqni qayta bajaring. Avvalgi `server-deploy` nusxasi agentning `skill-backups/` katalogiga saqlanadi; muvaffaqiyatsiz almashtirishda tiklanadi. Eski `safe-vps-deploy` nomli o‘rnatma bo‘lsa, yangisini tekshirgandan keyin uni skills katalogidan tashqariga zaxiralab ko‘chiring, ikkisini birga ishlatmang.

Codex’da yangi chat oching va `$server-deploy` deb chaqiring. Claude plugin sifatida ham ishlaydi:

```text
/plugin marketplace add xamidjonovk/safe-vps-deploy
/plugin install server-deploy@komiljon-skills
```

Skill fayllarini o‘rnatish serverga ulanmaydi va dasturlarni kompyuteringizga avtomatik o‘rnatmaydi. Python 3.11+, `uv`, Git va OpenSSH (`ssh-keygen`) kerak. `uv` yo‘q bo‘lsa: `python -m pip install uv`. Tekshirish: `uv --version`, `git --version`, `ssh -V`.

## Tez boshlash

Loyihaning gitga kiritilmagan lokal `.env` faylida:

```dotenv
SERVER=ssh://root@your-server.example:22
PAROL='your-bootstrap-password'
# Ixtiyoriy: provider panelidagi tekshirilgan SHA256 fingerprint
# SSH_HOST_FINGERPRINT=SHA256:...
```

`SERVER` uchun oddiy host, `user@host` yoki `ssh://user@host:port` ishlaydi. Ixtiyoriy `SSH_USER` va `SSH_PORT` qiymatlari URL sozlamalaridan ustun. Parol `$` yoki `#` belgilarini o‘z ichiga olsa bir tirnoq bilan yozing; u interpolatsiya qilinmaydi. `.env` ni commit qilmang.

Agentga yozing:

> $server-deploy — .env dagi SERVER va PAROL orqali SSH kirishni sozla, server holatini tekshir va boshqa loyihalarga halal bermay shu Git loyihani deploy qil.

Noma’lum host kalitida skript to‘xtab fingerprintni ko‘rsatadi. Uni provider yoki ishonchli kanal bilan tekshirib `SSH_HOST_FINGERPRINT` ga yozing. `--trust-new-host` birinchi uchragan kalitni saqlash imkonini beradi, ammo server kimligini mustaqil tasdiqlamaydi. Saqlangan host kaliti o‘zgarsa ulanish rad etiladi.

Birinchi tekshiruv read-only; `--apply` ruxsat berilgan SSH bootstrapni bajaradi. Bootstrap parolni faqat xotirada ishlatadi; argumentga, logga, GitHub’ga yoki serverdagi ilova `.env` fayliga ko‘chirmaydi. Root yoki sudo huquqi faqat shu bosqichda kerak. Keyingi kirish loyiha foydalanuvchisi va maxsus kalit bilan amalga oshiriladi. Hostning mavjud SSH/firewall sozlamalari o‘zgartirilmaydi.

Qo‘lda tekshirish (Codex o‘rnatmasi):

```powershell
uv run "$HOME/.codex/skills/server-deploy/scripts/bootstrap_ssh.py" --env-file .env --project my-app
uv run "$HOME/.codex/skills/server-deploy/scripts/bootstrap_ssh.py" --env-file .env --project my-app --apply
```

## Server talablari va deploy

Bootstrap mavjud server uchun ehtiyotkor: Python3 + PyYAML, sudo/visudo, Git, OpenSSH, Docker Engine va Compose oldindan ishlashi kerak. Nginx va Certbot domen/HTTPS bosqichida kerak. Yetishmayotgan paketlar topilsa aniq xato bilan to‘xtaydi; paketlarni avtomatik yangilamaydi yoki umumiy xizmatlarni restart qilmaydi. `server/install.sh` to‘liq host provisioning uchun alohida ko‘rib chiqiladigan eski vosita; u yengil bootstrap davomida chaqirilmaydi.

Har bir loyiha alohida Compose nomi, localhost porti, volume va foydalanuvchiga ega. Broker konteyner imtiyozlari va portlarni tekshiradi; boshqa loyihalarning data va Docker socketiga kirish taqiqlanadi. Ketma-ketlik: **test → Git commit/push → server sync → env/status → validate → migration → deploy → readiness → qo‘shni xizmatlar holatini solishtirish**. App nosoz bo‘lsa broker oxirgi yaxshi commitga rollback qiladi. Database migratsiyasi avtomatik orqaga qaytarilmaydi: zaxira va orqaga mos schema kerak. Birinchi deployda rollback uchun oldingi ishlagan release hali yo‘q.

Domen/DNS/HTTPS va tashqi SMS/payment kabi secretlar haqiqiy konfiguratsiya bilan alohida sozlanadi. GitHub Actions template cheklangan CI kaliti va SSH portini qo‘llaydi; loyiha testlarini deploy jobidan oldin ulash kerak. Skill doimiy CI sozlashni bir martalik deploy bilan adashtirmaydi.

Hech bir vosita barcha serverlarda mutlaq bexato yoki uzilishsiz deployni kafolatlay olmaydi. Muvaffaqiyat faqat readiness, HTTP javoblari, aniq commit SHA va mavjud xizmatlar holati tekshirilgandan keyin bildiriladi.

## Tekshirish va ishlab chiqish

```bash
git clone https://github.com/xamidjonovk/safe-vps-deploy.git
cd safe-vps-deploy
uv run --with paramiko --with python-dotenv python plugins/server-deploy/skills/server-deploy/scripts/test_bootstrap.py
```

Testlar haqiqiy VPS’ga ulanmaydi. Installerlar Windows va Ubuntu’da vaqtinchalik kataloglarda CI orqali tekshiriladi. Asosiy [SKILL.md](plugins/server-deploy/skills/server-deploy/SKILL.md), [deploy oqimi](plugins/server-deploy/skills/server-deploy/references/deploy-flow.md) va [server policy](plugins/server-deploy/skills/server-deploy/references/policy.md) ichida batafsil tartib bor.
