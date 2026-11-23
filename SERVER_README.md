# 🚀 Contabo Serverda Docker Orqali Ishga Tushirish Qo'llanmasi

Ushbu bot **Aiogram 3 (Long Polling)** rejimida ishlaydi. 
> 💡 **Muhim:** Bot tashqaridan kiruvchi (inbound) birorta port talab qilmaydi! Contabo serveringizdagi yopiq portlar botga mutlaqo to'sqinlik qilmaydi. Bot faqat chiquvchi HTTPS (Telegram API) orqali xavfsiz ishlaydi.

Loyihalar papkangiz: `/opt`
Bot manzili: `/opt/huquq_test_bot`

---

## 🛠 1. Serverga o'rnatish qadamlari

### 1-qadam: Papkaga o'tish va loyihani yuklash
Contabo serveringizga SSH orqali kiring:
```bash
cd /opt

# Git orqali klonlash:
git clone <sizning_repository_manzilingiz> huquq_test_bot

# Yoki fayllarni to'g'ridan-to'g'ri /opt/huquq_test_bot papkasiga yuklang
cd /opt/huquq_test_bot
```

### 2-qadam: `.env` faylini sozlash
`.env.example` dan nusxa oling va tokenni kiriting:
```bash
cp .env.example .env
nano .env
```
Quyidagi qiymatlarni to'ldiring:
```env
BOT_TOKEN=1234567890:AA...sizning_tokeningiz
ADMIN_IDS=sizning_telegram_id
SHUFFLE_QUESTIONS=1
```
*(Saqlash: `Ctrl + O`, `Enter`, Chiqish: `Ctrl + X`)*

### 3-qadam: Konteynerni ishga tushirish
Birgina buyruq bilan konteyner yig'iladi va fonda ishga tushadi:
```bash
docker compose up -d --build
```

---

## 📊 2. Botni boshqarish va kuzatish

- **Loglarni (jurnalni) jonli ko'rish:**
  ```bash
  docker compose logs -f
  ```
- **Bot holatini ko'rish:**
  ```bash
  docker compose ps
  ```
- **Botni qayta ishga tushirish (restart):**
  ```bash
  docker compose restart
  ```
- **Botni to'xtatish:**
  ```bash
  docker compose down
  ```

---

## 📂 3. Ma'lumotlar xavfsizligi va testlarni yangilash

Konteyner ichidagi ma'lumotlar hostdagi (`/opt/huquq_test_bot/`) papkalarga bog'langan (Docker Volume):
- `data/natijalar.db` — O'quvchilar natijalari saqlanadigan SQLite bazasi. Konteyner o'chsa yoki yangilansa ham ma'lumotlar o'chib ketmaydi.
- `tests/` — Testlar papkasi. Yangi `.json` test qo'shish uchun shunchaki shu papkaga tashlang va Telegramda adminga **/yangilash** buyrug'ini yuboring (konteynerni restart qilish shart emas!).
- `admins.txt` — Adminlar ro'yxati.
