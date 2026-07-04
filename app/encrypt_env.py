"""
CLI tool to encrypt environment secrets for .env.encrypted.

Usage:
    python -m app.encrypt_env <plaintext> <password>
    python -m app.encrypt_env "word1 word2 ... word12" "my password"

Output:
    buck_enc:<base64 payload>  — paste this as the value in .env
"""

import sys

from app.utils.crypto import encrypt_env


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python -m app.encrypt_env <plaintext> <password>")
        sys.exit(1)

    cipher = encrypt_env(sys.argv[1], sys.argv[2])
    print(f"buck_enc:{cipher}")


if __name__ == "__main__":
    main()
