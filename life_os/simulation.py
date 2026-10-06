"""Deterministic what-if comparison without executing real-world actions."""
from __future__ import annotations
import math
from typing import Any
def evaluate_options(options:list[dict[str,Any]])->list[dict[str,Any]]:
    if not isinstance(options,list) or not 1<=len(options)<=50: raise ValueError('options must contain 1..50 items')
    out=[]
    for option in options:
        if not isinstance(option,dict) or set(option)!={'name','outcomes','reversible'}: raise ValueError('invalid simulation option')
        name=str(option['name']).strip(); outcomes=option['outcomes']
        if not name or not isinstance(outcomes,list) or not outcomes: raise ValueError('option name and outcomes required')
        total=0.0; expected=0.0; worst=float('inf'); negative=0.0
        for item in outcomes:
            if not isinstance(item,dict) or set(item)!={'probability','value'}: raise ValueError('invalid simulation outcome')
            p=float(item['probability']); v=float(item['value'])
            if not math.isfinite(p) or not math.isfinite(v) or p<0 or p>1: raise ValueError('invalid simulation numbers')
            total+=p; expected+=p*v; worst=min(worst,v); negative += p if v<0 else 0
        if abs(total-1.0)>1e-6: raise ValueError('outcome probabilities must sum to 1')
        reversible=bool(option['reversible']); optionality=1.0 if reversible else 0.0
        out.append({'name':name,'expected_value':round(expected,6),'worst_case':worst,'probability_negative':round(negative,6),'reversible':reversible,'optionality':optionality})
    return sorted(out,key=lambda x:(-x['expected_value'],-x['optionality'],x['probability_negative'],x['name']))
