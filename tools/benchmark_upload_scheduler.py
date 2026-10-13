"""Safe scheduler benchmark: no Telegram API, files, MongoDB, or credentials."""

import argparse
import asyncio
import time


async def run_level(concurrency: int, transfers: int, part_mb: int, latency_ms: int) -> dict:
    gate = asyncio.Semaphore(concurrency)
    active = peak = 0

    async def transfer():
        nonlocal active, peak
        async with gate:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(latency_ms / 1000)
            active -= 1

    started = time.perf_counter()
    await asyncio.gather(*(transfer() for _ in range(transfers)))
    elapsed = time.perf_counter() - started
    total_mb = transfers * part_mb
    mb_s = total_mb / elapsed
    return {
        "concurrency": concurrency,
        "peak": peak,
        "seconds": elapsed,
        "mb_s": mb_s,
        "mbps": mb_s * 8,
        "gb_h": mb_s * 3600 / 1024,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default="1,4,8,16,28")
    parser.add_argument("--transfers", type=int, default=56)
    parser.add_argument("--part-mb", type=int, default=16)
    parser.add_argument("--latency-ms", type=int, default=5)
    args = parser.parse_args()
    print("mode=simulation; Telegram/MongoDB/local-files untouched")
    print("level peak seconds MB/s Mbps GB/h")
    for level in (int(value) for value in args.levels.split(",") if value.strip()):
        result = await run_level(level, args.transfers, args.part_mb, args.latency_ms)
        print(
            f"{result['concurrency']:>5} {result['peak']:>4} {result['seconds']:>7.3f} "
            f"{result['mb_s']:>6.1f} {result['mbps']:>6.1f} {result['gb_h']:>6.1f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
