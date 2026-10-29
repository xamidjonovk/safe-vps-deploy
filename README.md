# safe-vps-deploy

Claude Code yoki Codex uchun skill. U GitHub'dagi docker-compose loyihangizni VPS'ga **o'zi, to'liq va xavfsiz** deploy qiladi:
Dockerfile va compose, `.env`, nginx, domen, Let's Encrypt HTTPS, GitHub Actions CI/CD, health-check va avtomatik rollback.

Asosiy farqi: AI **root parolni olmaydi**, **secretlarni ko'rmaydi** va serverdagi **boshqa loyihalarga tega olmaydi**.

---

## 1. O'rnatish — bitta buyruq

### macOS / Linux
```bash
curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash
```

### Windows (PowerShell)
```powershell
irm https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.ps1 | iex
```

Skill bir vaqtning o'zida **Claude Code** (`~/.claude/skills`) va **Codex** (`~/.agents/skills`) uchun o'rnatiladi.
Yangilash uchun xuddi shu buyruqni qayta ishga tushiring.

<details>
<summary>Muqobil yo'l: Claude Code plugin sifatida</summary>

Claude Code ichida yozing:
```
/plugin marketplace add xamidjonovk/safe-vps-deploy
/plugin install safe-vps-deploy@komiljon-skills
```
Keyinchalik yangilash: `/plugin marketplace update komiljon-skills`
</details>

<details>
<summary>Faqat bittasiga o'rnatish yoki o'chirish</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash -s -- --claude     # faqat Claude Code
curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash -s -- --codex      # faqat Codex
curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash -s -- --uninstall  # o'chirish
```
Windows'da o'chirish: `$env:SVD_UNINSTALL=1; irm https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.ps1 | iex`
</details>

## 2. Ishlatish

Loyiha papkasida **yangi** Claude Code (yoki Codex) sessiyasini oching va shunday yozing:

> Loyihamni VPS'ga deploy qil. IP: 1.2.3.4, domen: app.misol.uz (frontend), api.misol.uz (backend)

Qolganini AI bosqichma-bosqich o'zi bajaradi. Sizdan faqat quyidagilar so'raladi:

1. Domen DNS'ida A record'ni server IP'siga yo'naltirish.
2. AI bergan 3 ta buyruqni **o'z terminalingizda** ishga tushirish. Root parolni o'zingiz kiritasiz, AI uni ko'rmaydi.
3. Tashqi API kalitlarini (to'lov, SMS va hokazo) yashirin rejimda kiritish.
4. Ish tugagach, AI'ning kirishini yopish: `ssh root@IP sdp-admin revoke-agent <loyiha>`.

Shundan keyin `main` branch'ga har bir `git push` avtomatik deploy bo'ladi.

## Talablar

- Claude Code yoki Codex
- Ubuntu yoki Debian VPS (Contabo, Hetzner, DigitalOcean va boshqalar)
- GitHub'dagi loyiha. Docker-compose bo'lmasa, AI uni o'zi yaratadi
- Domen (yoki subdomen)

Batafsil: qanday ishlashi, xavfsizlik modeli, cheklovlar va kundalik buyruqlar uchun
[README.uz.md](plugins/safe-vps-deploy/skills/safe-vps-deploy/README.uz.md) ga qarang.

---

### O'qituvchi uchun: repo'ni e'lon qilish (bir marta)

```bash
gh auth login        # agar hali kirmagan bo'lsangiz
bash publish.sh      # public repo yaratadi, OWNER o'rniga username'ingizni qo'yadi va push qiladi
```
Skript oxirida o'quvchilarga yuboriladigan tayyor buyruqlarni chiqaradi.

Yangi versiya chiqarish: fayllarni o'zgartiring, `VERSION` va `plugins/safe-vps-deploy/.claude-plugin/plugin.json` ichidagi `version` ni oshiring, keyin `git commit -am "..." && git push`.
