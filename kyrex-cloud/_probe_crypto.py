"""Scratch probe — DELETE AFTER USE."""
try:
    import cryptography
    print("cryptography", cryptography.__version__)
except Exception as e:
    print("MISSING:", type(e).__name__, e)
