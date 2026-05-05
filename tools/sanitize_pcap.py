"""Sanitize PII from a Miracast capture before committing to a public repo.

Performs same-length byte replacements so packet lengths and TCP sequence
numbers stay consistent (important for Wireshark dissection).

Usage:
    python tools/sanitize_pcap.py <input.pcap> <output.pcap>
"""

import sys


# ---------------------------------------------------------------------------
# Replacement table
# Each entry: (original_bytes, replacement_bytes)
# All pairs MUST be the same length.
# ---------------------------------------------------------------------------

REPLACEMENTS: list[tuple[bytes, bytes]] = [
    # --- MAC addresses (binary, as they appear in Ethernet headers) ---
    (bytes.fromhex("0e02bd4200f6"), bytes.fromhex("aabbcc112233")),  # laptop
    (bytes.fromhex("a2b339782747"), bytes.fromhex("aabbcc445566")),  # Tab S8+

    # --- MAC addresses (text form, as they appear in mDNS/DHCP payloads) ---
    (b"0e:02:bd:42:00:f6", b"aa:bb:cc:11:22:33"),
    (b"a2:b3:39:78:27:47", b"aa:bb:cc:44:55:66"),

    # --- Android device hostname (serial-derived) ---
    (b"Android_0DFXPP1C", b"Android_XXXXXXXX"),

    # --- KDE Connect device ID (32 hex chars) ---
    (b"2e2bdb6cf79c41d08990ae170ec4aa09", b"00000000000000000000000000000000"),

    # --- KDE Connect service instance name ---
    (b"I0oyMVD8n14AAA", b"AAAAAAAAAAAAAA"),

    # --- KDE Connect service type prefix ---
    (b"_FC9F5ED42C8A", b"_000000000000"),

    # --- KDE Connect identity key in TXT record ---
    (b"NPGRMdBBP2AQq-YcdwC3VD0", b"AAAAAAAAAAAAAAAAAAAAAAA"),

    # --- User's name in intel_friendly_name ---
    (b"JUAN's Tab S8+", b"User's Tab S8+"),

    # --- Windows Miracast source GUID ---
    (b"57830206-DBCB-0002-E44B-CA57CBDBDC01",
     b"00000000-0000-0002-0000-000000000001"),

    # --- Samsung SSDP device UUID ---
    (b"e82dc3ae-baf5-4ad3-97dd-4c2230ac963b",
     b"00000000-0000-0000-0000-000000000002"),

    # --- Tab's real home-network IP (in KDE Connect mDNS TXT) ---
    (b"192.168.12.49", b"192.168.99.99"),
]


def _validate() -> None:
    for orig, repl in REPLACEMENTS:
        if len(orig) != len(repl):
            raise ValueError(
                f"Length mismatch: {orig!r} ({len(orig)}) "
                f"vs {repl!r} ({len(repl)})"
            )


def sanitize(data: bytearray) -> tuple[bytearray, dict[bytes, int]]:
    counts: dict[bytes, int] = {}
    for orig, repl in REPLACEMENTS:
        count = 0
        start = 0
        while (idx := data.find(orig, start)) != -1:
            data[idx : idx + len(orig)] = repl
            start = idx + len(repl)
            count += 1
        if count:
            counts[orig] = count
    return data, counts


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} <input.pcap> <output.pcap>")

    _validate()

    in_path, out_path = sys.argv[1], sys.argv[2]
    with open(in_path, "rb") as f:
        data = bytearray(f.read())

    original_len = len(data)
    data, counts = sanitize(data)

    assert len(data) == original_len, "BUG: sanitize changed file length"

    with open(out_path, "wb") as f:
        f.write(data)

    print(f"Wrote {out_path} ({original_len} bytes)")
    print("Replacements made:")
    for orig, count in counts.items():
        print(f"  {orig!r:50s} x{count}")

    missed = [orig for orig, _ in REPLACEMENTS if orig not in counts]
    if missed:
        print("\nNOT FOUND (verify manually):")
        for m in missed:
            print(f"  {m!r}")


if __name__ == "__main__":
    main()
