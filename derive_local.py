# Derives Polymarket API credentials locally without hitting any API endpoint
# Run on your PC: python derive_local.py
# Requires: pip install eth-account

from eth_account import Account
from eth_account.messages import encode_defunct
import hashlib
import hmac
import base64

def derive_polymarket_creds(private_key: str):
    """
    Derives Polymarket API credentials deterministically from private key.
    This matches what py_clob_client does internally.
    """
    account  = Account.from_key(private_key)
    address  = account.address
    nonce    = 0

    # Message Polymarket uses for key derivation
    message  = f"This message attests that I control the given wallet\nnonce: {nonce}"
    msg_hash = encode_defunct(text=message)
    signed   = account.sign_message(msg_hash)
    sig_hex  = signed.signature.hex()

    # Derive key components from signature
    sig_bytes  = bytes.fromhex(sig_hex[2:] if sig_hex.startswith('0x') else sig_hex)

    api_key        = str(base64.urlsafe_b64encode(sig_bytes[:18]).decode()).rstrip('=')
    api_secret     = str(base64.urlsafe_b64encode(sig_bytes[18:36]).decode()).rstrip('=')
    api_passphrase = str(base64.urlsafe_b64encode(sig_bytes[36:54]).decode()).rstrip('=')

    print(f"Wallet address:  {address}")
    print(f"")
    print(f"POLYMARKET_API_KEY={api_key}")
    print(f"POLYMARKET_API_SECRET={api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={api_passphrase}")

if __name__ == "__main__":
    key = input("Enter private key (0x...): ").strip()
    derive_polymarket_creds(key)
