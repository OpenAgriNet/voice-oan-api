import jwt
import os
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from fastapi.security.utils import get_authorization_scheme_param
from helpers.utils import get_logger
from app.config import settings # Import the application settings

load_dotenv()

logger = get_logger(__name__)

class OptionalOAuth2PasswordBearer(OAuth2PasswordBearer):
    """OAuth2 scheme that's optional in development"""
    async def __call__(self, request: Request) -> str | None:
        if settings.environment == "development":
            authorization = request.headers.get("Authorization")
            if not authorization:
                raise HTTPException(status_code=401, detail="Unauthorized")
            scheme, param = get_authorization_scheme_param(authorization)
            if scheme.lower() != "bearer":
                return None
            return param
        # In production, use normal OAuth2 behavior
        return await super().__call__(request)

# OAuth2 scheme for FastAPI - optional in development
oauth2_scheme = OptionalOAuth2PasswordBearer(tokenUrl="token")

# Construct the absolute path to the public key using settings
public_key_path = settings.base_dir / settings.jwt_public_key_path

with open(public_key_path, 'rb') as key_file:
    public_key = serialization.load_pem_public_key(key_file.read())
logger.info(f"Successfully loaded JWT Public Key from: {public_key_path}")


def _jwt_issuer_values() -> list[str]:
    raw = (settings.jwt_issuer or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


async def get_current_user(token: str | None = Depends(oauth2_scheme)):
    """
    FastAPI dependency to get current authenticated user from JWT token.
    This replaces the Django middleware approach.
    """
  
    
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    if public_key is None:
        logger.error("JWT Public Key is not loaded, cannot verify tokens.")
        raise credentials_exception
        
    try:
        issuers = _jwt_issuer_values()
        verify_aud = bool((settings.jwt_audience or "").strip())
        verify_iss = bool(issuers)

        optional: dict = {}
        if verify_aud:
            optional["audience"] = settings.jwt_audience.strip()
        if verify_iss:
            optional["issuer"] = issuers[0] if len(issuers) == 1 else issuers

        decoded_token = jwt.decode(
            token,
            public_key,
            algorithms=[settings.jwt_algorithm],
            options={
                "verify_signature": True,
                "verify_aud": verify_aud,
                "verify_iss": verify_iss,
            },
            **optional,
        )
        
        logger.info(f"Decoded token: {decoded_token}")
        
        return decoded_token
        
    except jwt.ExpiredSignatureError:
        logger.warning("Token has expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    except Exception as e:
        logger.error(f"Unexpected error during token verification: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed",
            headers={"WWW-Authenticate": "Bearer"},
        )