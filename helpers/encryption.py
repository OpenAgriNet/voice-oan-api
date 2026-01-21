"""
Encryption utilities for PM-KISAN grievance system
Handles AES-GCM encryption and decryption
"""

import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def hex_to_bytes(hex_string: str) -> bytes:
    """Convert hex string to bytes"""
    return bytes.fromhex(hex_string)


def encrypt_aes_gcm(plaintext: str, key: bytes, iv: bytes) -> str:
    """
    Encrypt plaintext using AES-GCM encryption
    
    Args:
        plaintext: The text to encrypt
        key: 32-byte encryption key
        iv: 12-byte initialization vector
    
    Returns:
        Base64 encoded encrypted data
    """
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(iv, plaintext.encode('utf-8'), None)  # ciphertext = encrypted data
    return base64.b64encode(ciphertext).decode('utf-8')


def decrypt_aes_gcm(encrypted_data: str, key: bytes, iv: bytes) -> str:
    """
    Decrypt AES-GCM encrypted data
    
    Args:
        encrypted_data: Base64 encoded encrypted data
        key: 32-byte decryption key
        iv: 12-byte initialization vector
    
    Returns:
        Decrypted plaintext
    """
    aesgcm = AESGCM(key)
    ciphertext = base64.b64decode(encrypted_data)
    plaintext = aesgcm.decrypt(iv, ciphertext, None)
    return plaintext.decode('utf-8')
