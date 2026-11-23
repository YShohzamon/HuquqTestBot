# Ba'zi hostinglar asosiy fayl sifatida main.py ni qidiradi.
# Bu fayl shunchaki bot.py ni ishga tushiradi.
import asyncio
from bot import main

if __name__ == "__main__":
    asyncio.run(main())
