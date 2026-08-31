import secrets
import sys

def generate_api_key(length=32):
    """Generate a cryptographically secure random API key"""
    return secrets.token_urlsafe(length)

if __name__ == "__main__":
    key = generate_api_key()
    print(f"Generated API Key: {key}")
    print(f"Length: {len(key)} characters")
    print("\nAdd to .env:")
    print(f"API_KEY={key}")