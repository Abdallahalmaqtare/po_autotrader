"""Optional real Atlas connectivity probe. Does not read/write bot_settings.
Run manually after supplying MONGO_URI: python check_mongo.py
"""
import asyncio
from pymongo import AsyncMongoClient
import config


async def main():
    if not config.MONGO_URI or '<' in config.MONGO_URI:
        raise SystemExit('Fill MONGO_URI with your own Atlas credentials first')
    client = AsyncMongoClient(config.MONGO_URI, serverSelectionTimeoutMS=8000,
                              connectTimeoutMS=8000, socketTimeoutMS=8000)
    try:
        await client.admin.command('ping')
        print('MongoDB ping succeeded; no settings documents were modified.')
    except Exception as e:
        print('MongoDB ping failed:', type(e).__name__)
        raise SystemExit(1) from None
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
