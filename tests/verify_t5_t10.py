import asyncio
import sys
import test_fuguoai_backend as T


async def main():
    r5 = await T.test_flood(8, "test-flood")
    r10 = await T.test_v3_observability()
    print(f"RESULT T5={r5} T10={r10}")
    sys.exit(0 if (r5 and r10) else 1)


asyncio.run(main())
