# safe-vps-deploy — AI uchun xavfsiz deploy skill'i

Claude Code (yoki Codex) GitHub'dagi docker-compose loyihasini (FastAPI, Next.js, Postgres, Redis,
Celery, MinIO va boshqalar) VPS'ga **to'liq o'zi** deploy qiladi:
production Dockerfile/compose, `.env`, nginx, domen, Let's Encrypt HTTPS, GitHub Actions CI/CD,
health-check va avtomatik rollback.

Eng muhimi shundaki, AI **root parolni hech qachon olmaydi**, **secret qiymatlarni ko'rmaydi** va
serverdagi **boshqa loyihalarga tega olmaydi**.

## Qanday ishlaydi

Serverga `sdp` (Safe Deploy Proxy) nomli kichik vositachi o'rnatiladi:

- Har bir loyiha uchun alohida Linux user ochiladi: `sdp-<loyiha>`. AI faqat shu user bilan, SSH kalit orqali kiradi.
- Bu userda root huquqi yo'q va `docker` guruhida ham emas. Uning yagona imtiyozi: `sudo sdp <buyruq>`.
  Bunda `sdp` loyihani login'dan aniqlaydi, shuning uchun AI faqat o'z loyihasini boshqara oladi.
- `.env` fayli `/srv/apps/<loyiha>/.env` da turadi, faqat root o'qiy oladi (0600). AI kalitlarni
  yaratadi va random secretlar generatsiya qiladi, lekin qiymatlarni ko'rmaydi. Loglarda secretlar `***` bilan yashiriladi.
- Har bir deploydan oldin compose fayli **xavfsizlik siyosati**dan o'tkaziladi. Quyidagilar taqiqlanadi:
  `privileged`, docker.sock, host papkalari, host network, ommaviy portlar (faqat `127.0.0.1` ruxsat etiladi),
  boshqa loyihalarning volume, network va image'lari. Shu sababli bitta loyiha serverni yoki qo'shni loyihani buza olmaydi.
- Nginx'da faqat o'z domen fayllari yoziladi. Boshqa saytlar bilan to'qnashuv tekshiriladi, `nginx -t` xato bersa o'zgarish orqaga qaytariladi.
- Deploy jarayoni: build → ishga tushirish → health-check. Muvaffaqiyatsiz bo'lsa, oxirgi ishlagan versiyaga **avtomatik rollback** qilinadi.
- GitHub Actions'dagi SSH kalit "forced command" bilan cheklangan: u **faqat** deployni ishga tushira oladi, shell ochib bo'lmaydi.

## O'rnatish (bir marta)

Bitta buyruq (Claude Code va Codex uchun birdaniga o'rnatadi):

- **macOS / Linux:** `curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash`
- **Windows (PowerShell):** `irm https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.ps1 | iex`

Yoki Claude Code ichida: `/plugin marketplace add xamidjonovk/safe-vps-deploy`, keyin `/plugin install safe-vps-deploy@komiljon-skills`.

Ixtiyoriy: `templates/claude-code/settings.json` ni loyihadagi `.claude/settings.json` ga qo'shsangiz,
Claude Code lokal `.env` fayllarni o'qishdan bloklanadi. Codex uchun `templates/AGENTS.md.snippet` bor.

## Ishlatish

Claude Code'da loyiha papkasini ochib, quyidagini yozing:

> Loyihamni Contabo VPS'ga deploy qil. IP: 1.2.3.4, domen: app.misol.uz (frontend), api.misol.uz (backend)

AI ishni bosqichma-bosqich bajaradi. Talaba atigi **4 ta ishni qo'lda** qiladi:

1. Domen DNS'ida A record'ni server IP'siga yo'naltiradi.
2. AI bergan 3 ta buyruqni **o'z terminalida** ishga tushiradi va root parolni o'zi kiritadi:
   ```bash
   scp -r ~/.claude/skills/safe-vps-deploy/server root@IP:/root/sdp-installer   # AI to'g'ri yo'lni o'zi beradi
   ssh root@IP "bash /root/sdp-installer/install.sh"
   ssh root@IP "sdp-admin add-project myshop --agent-key '$(cat ~/.ssh/sdp_myshop.pub)'"
   ```
3. Tashqi API kalitlarini (to'lov, SMS, OpenAI va hokazo) o'zi kiritadi. Kiritilgan matn ekranda ko'rinmaydi:
   ```bash
   ssh root@IP sdp-admin secret myshop PAYME_SECRET_KEY
   ```
4. Ish tugagach, AI'ning server'ga kirishini yopadi. CI/CD bundan keyin ham ishlayveradi:
   ```bash
   ssh root@IP sdp-admin revoke-agent myshop
   ```

Shundan keyin `main` branch'ga har bir `git push` avtomatik deploy bo'ladi.

## Kundalik buyruqlar (server'da root sifatida)

```bash
sdp-admin list                         # loyihalar, agent kirishi, domenlar
sdp -p myshop status                   # holat, konteynerlar, sertifikat muddati
sdp -p myshop logs backend --tail 100  # loglar
sdp -p myshop deploy                   # qo'lda deploy
sdp-admin grant-agent myshop --agent-key '...'   # AI'ga kirishni qayta berish
```

## Halol cheklovlar

- `sdp` server'ni, boshqa loyihalarni va secretlarning **tasodifan** AI kontekstiga yoki chatga tushib qolishini himoya qiladi.
  Ammo deploy qilinadigan kodni AI yozadi. Nazariy jihatdan u ataylab secretni ilova orqali chiqarib yuboradigan kod yozishi mumkin.
  Shuning uchun commit'larni ko'rib chiqing va ish tugagach `revoke-agent` qiling.
- Rollback faqat kodni qaytaradi, **DB migratsiyalari qaytarilmaydi**.
- Backup avtomatik emas. Postgres uchun `pg_dump` cron yoki Contabo snapshot'larini sozlang.
- Agar 80/443 portlarni nginx emas, boshqa narsa (Caddy, Traefik, docker'dagi proxy) egallagan bo'lsa,
  `install.sh` ishlamaydi. Bu holda server egasi bilan kelishish kerak.
- Faqat Ubuntu/Debian qo'llab-quvvatlanadi.

## Tarkib

```
SKILL.md                         AI uchun to'liq yo'riqnoma (bosqichlar, qoidalar, buyruqlar)
server/install.sh                server'ga bir marta o'rnatish (root, odam bajaradi)
server/sdp, sdp-admin, sdp-ci-entry, sdp_core.py
templates/docker-compose.prod.yml  FastAPI+Next.js+Postgres+Redis+Celery+MinIO namunasi
templates/Dockerfile.fastapi, Dockerfile.nextjs, dockerignore.txt
templates/github-workflows/deploy.yml
templates/claude-code/settings.json, AGENTS.md.snippet
references/policy.md, troubleshooting.md
```
