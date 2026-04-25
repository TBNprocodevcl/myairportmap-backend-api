from pydantic import BaseModel
from typing import Optional, Dict
from datetime import date

class RunwayInput(BaseModel):
    airport_id: str
    date: Optional[date]
    aircraft: Optional[str]
    notes: Optional[str]


class Runway360SaveRequest(BaseModel):
    data: Dict[int, Optional[RunwayInput]]
