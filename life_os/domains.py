"""Life-domain registry and defaults."""
from .ontology import DOMAIN_ALIASES, DOMAIN_REGISTRY

LEGACY_DOMAINS=("health","fitness","nutrition","sleep","money","work","learning","relationships","recreation","transport","home","privacy","resilience")
DOMAINS=tuple(dict.fromkeys((*DOMAIN_REGISTRY.keys(),*LEGACY_DOMAINS)))

DEFAULT_METRICS={
 "sleep":(("duration","hours"),("quality","score")),
 "fitness":(("weight","lb"),("waist","in"),("training","minutes")),
 "health":(("energy","score"),("pain","score")),
 "nutrition":(("protein","g"),("water","ml")),
 "learning":(("focused_learning","minutes"),),
 "recreation":(("recreation","minutes"),),
}
