"""
Delilah Financial OS - Transaction Models

Pydantic models for transaction validation and serialization.
"""

from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field, field_validator, ConfigDict
from enum import Enum


class TransactionStatus(str, Enum):
    """Transaction status enumeration."""
    PENDING_EVALUATION = "Pending Evaluation"
    EVALUATED = "Evaluated"
    CORRECTED = "Corrected"
    REMOVED = "Removed"
    FLAGGED = "Flagged"


class TransactionJudgment(str, Enum):
    """Transaction judgment types."""
    APPROVED = "approved"
    FLAGGED = "flagged"
    REQUIRES_REVIEW = "requires_review"
    REJECTED = "rejected"


class TransactionBase(BaseModel):
    """Base transaction model with common fields."""
    
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
    )
    
    user_id: str = Field(..., min_length=1, max_length=256)
    merchant: str = Field(..., min_length=1, max_length=500)
    amount: float = Field(..., gt=-1000000, lt=1000000)
    account_used: str = Field(..., min_length=1, max_length=256)
    date: str = Field(..., pattern=r'^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$')
    
    @field_validator('merchant')
    @classmethod
    def sanitize_merchant(cls, v: str) -> str:
        """Sanitize merchant name."""
        # Remove null bytes and trim
        v = v.replace('\x00', '').strip()
        if not v:
            raise ValueError("Merchant name cannot be empty")
        return v
    
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Validate date format."""
        try:
            if ' ' in v:
                datetime.strptime(v, '%Y-%m-%d %H:%M:%S')
            else:
                datetime.strptime(v, '%Y-%m-%d')
        except ValueError:
            raise ValueError("Invalid date format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
        return v


class TransactionCreate(TransactionBase):
    """Model for creating a new transaction."""
    
    transaction_id: str = Field(..., min_length=1, max_length=256)
    pending_transaction_id: Optional[str] = Field(default=None, max_length=256)
    total_liquid: float = Field(default=0.0, ge=-10000000, le=10000000)
    net_cash: str = Field(default="", max_length=50)
    status: TransactionStatus = Field(default=TransactionStatus.PENDING_EVALUATION)
    
    @field_validator('transaction_id')
    @classmethod
    def validate_transaction_id(cls, v: str) -> str:
        """Validate transaction ID format."""
        v = v.strip()
        if not v:
            raise ValueError("Transaction ID cannot be empty")
        # Allow alphanumeric, underscores, hyphens
        import re
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError("Transaction ID contains invalid characters")
        return v


class TransactionUpdate(BaseModel):
    """Model for updating an existing transaction."""
    
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
    )
    
    merchant: Optional[str] = Field(default=None, min_length=1, max_length=500)
    category: Optional[str] = Field(default=None, max_length=256)
    status: Optional[TransactionStatus] = None
    judgment: Optional[str] = Field(default=None, max_length=500)
    override_amount: Optional[float] = Field(default=None, gt=-1000000, lt=1000000)
    override_reason: Optional[str] = Field(default=None, max_length=1000)
    
    @field_validator('merchant')
    @classmethod
    def sanitize_merchant(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize merchant name."""
        if v is None:
            return v
        v = v.replace('\x00', '').strip()
        if not v:
            raise ValueError("Merchant name cannot be empty")
        return v


class TransactionResponse(TransactionBase):
    """Model for transaction API responses."""
    
    id: int
    transaction_id: str
    message_id: Optional[int] = None
    clean_merchant: Optional[str] = None
    category: Optional[str] = None
    total_liquid: float
    net_cash: str
    all_balances: Optional[str] = None
    status: TransactionStatus
    judgment: Optional[str] = None
    raw_embed: Optional[str] = None
    pending_transaction_id: Optional[str] = None
    override_amount: Optional[float] = None
    override_reason: Optional[str] = None
    override_at: Optional[str] = None
    override_source: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)


class TransactionBatchCreate(BaseModel):
    """Model for batch transaction creation."""
    
    transactions: List[TransactionCreate] = Field(
        ..., 
        min_length=1, 
        max_length=100
    )
    
    @field_validator('transactions')
    @classmethod
    def validate_batch_size(cls, v: List[TransactionCreate]) -> List[TransactionCreate]:
        """Validate batch size."""
        if len(v) > 100:
            raise ValueError("Batch size cannot exceed 100 transactions")
        return v


class TransactionFilter(BaseModel):
    """Model for filtering transactions."""
    
    user_id: Optional[str] = None
    status: Optional[TransactionStatus] = None
    merchant: Optional[str] = None
    category: Optional[str] = None
    date_from: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    date_to: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    amount_min: Optional[float] = Field(default=None, ge=0)
    amount_max: Optional[float] = Field(default=None, ge=0)
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    
    @field_validator('amount_max')
    @classmethod
    def validate_amount_range(cls, v: Optional[float], info) -> Optional[float]:
        """Validate amount range."""
        values = info.data
        if v is not None and values.get('amount_min') is not None:
            if v < values['amount_min']:
                raise ValueError("amount_max must be >= amount_min")
        return v


__all__ = [
    'TransactionStatus',
    'TransactionJudgment',
    'TransactionBase',
    'TransactionCreate',
    'TransactionUpdate',
    'TransactionResponse',
    'TransactionBatchCreate',
    'TransactionFilter',
]
