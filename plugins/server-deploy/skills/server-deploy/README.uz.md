# Server deploy

`server-deploy` Git loyihani boshqa ilovalar ishlayotgan VPS’ga joylashtirish uchun yo‘riqnoma va yordamchi skriptlarni beradi.

Lokal `.env` dagi `SERVER` va `PAROL` faqat `scripts/bootstrap_ssh.py` ichida o‘qiladi. Dastlab server holati tekshiriladi; `--apply` loyiha uchun Ed25519 kalit hamda `sdp-<loyiha>` foydalanuvchisini yaratadi. Keyingi deploylar cheklangan `sudo sdp` vositachisi orqali bajariladi. Parol argumentlarga, loglarga yoki serverdagi ilova `.env` fayliga yozilmaydi.

```powershell
uv run <skill-path>/scripts/bootstrap_ssh.py --env-file .env --project crm-qurlish
uv run <skill-path>/scripts/bootstrap_ssh.py --env-file .env --project crm-qurlish --apply
```

`SERVER=host`, `SERVER=user@host` yoki `SERVER=ssh://user@host:port` ishlaydi. Ixtiyoriy sozlamalar: `SSH_USER`, `SSH_PORT`, `SSH_HOST_FINGERPRINT`. Noma’lum host kaliti avval tekshiriladi; `--trust-new-host` faqat ongli birinchi ulanish ishonchi uchun. Saqlangan kalit o‘zgarsa ulanish rad etiladi.

Bootstrap paket o‘rnatmaydi, firewall/SSH sozlamalarini o‘zgartirmaydi, boshqa xizmatlarni qayta ishga tushirmaydi. Yetishmayotgan dependency yoki farqli umumiy broker topilsa to‘xtaydi. To‘liq jarayon uchun `SKILL.md`, xatolar uchun `references/` ga qarang.
