# register_keys.py
# Connects to Polymarket's backend to officially create and authorize L2 API keys.

import os
from py_clob_client_v2.client import ClobClient
from dotenv import load_dotenv

load_dotenv()

def main():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not pk:
        pk = input("Enter your Private Key: ").strip()

    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip()
    funder_addr = None
    if proxy:
        from web3 import Web3
        try:
            funder_addr = Web3.to_checksum_address(proxy)
        except Exception:
            funder_addr = proxy

    print("Connecting to Polymarket to register API keys...")
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        signature_type=2,
        funder=funder_addr
    )

    try:
        # Try to derive first (often more reliable), fallback to create
        # Force create first so Polymarket's backend explicitly registers the proxy
        try:
            creds = client.create_api_key()
        except Exception:
            creds = client.derive_api_key()
            
        print("\n✅ Keys successfully generated AND registered on Polymarket!\n")
        print("Update your .env file with these exact values:\n")
        print(f"POLYMARKET_API_KEY={creds.api_key}")
        print(f"POLYMARKET_API_SECRET={creds.api_secret}")
        print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    except Exception as e:
        if "400" in str(e) or "Could not create api key" in str(e):
            print("\n❌ Error 400: Polymarket rejected the key creation.")
            print("\nBecause you generated this private key in Colab, Polymarket doesn't know it exists yet!")
            print("You must activate the wallet on their website before their API will let you trade.\n")
            print("HOW TO FIX:")
            print(" 1. Import your new Private Key into MetaMask.")
            print(" 2. Go to Polymarket.com and connect this MetaMask wallet.")
            print(" 3. Sign the welcome message to accept the Terms of Service.")
            print(" 4. Deposit a few dollars of USDC (Polygon) to deploy your Proxy Wallet.")
            print(" 5. Run this script again.\n")
        else:
            print(f"\n❌ Error registering keys: {e}")

if __name__ == "__main__":
    main()