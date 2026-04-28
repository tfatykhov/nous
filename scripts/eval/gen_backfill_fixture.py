"""F056 PR #3: regenerate tests/fixtures/handlers/backfill_corpus.jsonl.

One-shot script. Run from repo root:

    uv run python scripts/eval/gen_backfill_fixture.py

Produces 100 facts across 10 unrelated domains (10 each). 33% flagged
`is_orphan_intent=true` — informational only in PR #3 v1, since all
facts start as orphans (no edges pre-seeded). All `reviewed_by="tim"`.

Committed under scripts/eval/ rather than left as a transient artifact
so future fixture changes (e.g. F056.2 mixed entity types) can be
applied via diff against this script.
"""
import json
from pathlib import Path

DOMAINS = {
    "astronomy": [
        "Jupiter has four large Galilean moons named Io Europa Ganymede and Callisto.",
        "The Andromeda galaxy lies approximately 2.5 million light-years from Earth.",
        "A solar eclipse happens when the moon passes between Earth and the sun.",
        "Saturn rings are mostly water ice with some rocky particulate material.",
        "The Hubble Space Telescope was launched into low Earth orbit in 1990.",
        "Mars surface contains iron oxide which gives the planet its red color.",
        "Black holes warp spacetime so severely that not even light can escape.",
        "The Milky Way galaxy contains roughly 100 billion stars in a spiral disk.",
        "Pluto was reclassified from a planet to a dwarf planet back in 2006.",
        "Neutron stars pack the mass of the sun into a sphere ten kilometers across.",
    ],
    "cooking": [
        "Sourdough bread requires a starter culture of wild yeast and lactobacilli.",
        "Caramelization of onions takes about thirty to forty-five minutes over low heat.",
        "Fish sauce is a fermented condiment central to many Southeast Asian cuisines.",
        "Tempering chocolate involves heating and cooling to specific temperature ranges.",
        "Risotto is made by gradually adding warm broth while stirring arborio rice.",
        "A French roux thickens sauces by combining flour and butter cooked together.",
        "Sushi rice is seasoned with rice vinegar sugar and salt before forming nigiri.",
        "Brining poultry in salt water before roasting helps retain interior moisture.",
        "Pickling preserves vegetables in vinegar brine for several months at minimum.",
        "A perfect medium-rare steak reaches an internal temperature of 130 to 135 F.",
    ],
    "geography": [
        "The Mariana Trench is the deepest known oceanic trench in the Pacific.",
        "Mount Kilimanjaro is the highest mountain in Africa located in Tanzania.",
        "The Amazon River discharges more water than any other river in the world.",
        "Iceland sits on the Mid-Atlantic Ridge between two tectonic plate boundaries.",
        "Lake Baikal in Siberia holds about twenty percent of the worlds fresh water.",
        "The Sahara is the largest hot desert in the world covering northern Africa.",
        "The Ganges river is sacred in Hinduism flowing through India and Bangladesh.",
        "New Zealand sits on the boundary of the Pacific and Australian tectonic plates.",
        "The Mississippi river drains the central United States from north to south.",
        "Mount Everest reaches 8848 meters making it the tallest mountain on Earth.",
    ],
    "music": [
        "A grand piano typically has eighty-eight keys spanning seven full octaves.",
        "Bach composed the Brandenburg Concertos as a gift for the Margrave of Brandenburg.",
        "The Stradivarius violins were crafted in Cremona Italy during the 1700s.",
        "Reggae music originated in Jamaica during the late nineteen sixties.",
        "Miles Davis pioneered cool jazz with his nineteen fifty-nine album Kind of Blue.",
        "Beethoven composed his ninth symphony while almost completely deaf at the time.",
        "The Beatles released their first studio album titled Please Please Me in 1963.",
        "Hip hop emerged from block parties in the South Bronx during the early 1970s.",
        "Mozart composed over six hundred works during his short thirty-five year life.",
        "The Tibetan singing bowl produces a sustained tone through circular friction.",
    ],
    "animals": [
        "Octopuses have three hearts and blue blood due to copper-based hemocyanin.",
        "The Arctic tern migrates from polar region to polar region each year.",
        "Honey bees communicate the location of nectar sources via the waggle dance.",
        "Cheetahs are the fastest land animals capable of bursts above seventy mph.",
        "Komodo dragons can grow over three meters long on the islands of Indonesia.",
        "Elephants display empathy and mourn their dead in observed funeral behavior.",
        "Dolphins use echolocation clicks to navigate and find prey underwater.",
        "Wolves live in family-based packs with dominant breeding pair at the center.",
        "Hummingbirds beat their wings up to eighty times per second during hover.",
        "Polar bears have black skin under their translucent white-appearing fur coat.",
    ],
    "history": [
        "The Rosetta Stone enabled scholars to decipher Egyptian hieroglyphic writing.",
        "Marco Polo traveled along the Silk Road to the court of Kublai Khan in China.",
        "The Treaty of Westphalia in sixteen forty-eight ended the Thirty Years War.",
        "Cleopatra was the last active ruler of the Ptolemaic Kingdom of ancient Egypt.",
        "The printing press was invented by Johannes Gutenberg around fourteen forty.",
        "The Roman Empire fell in the west in four seventy-six AD according to historians.",
        "Genghis Khan unified Mongol tribes and built the largest contiguous empire ever.",
        "The French Revolution began in seventeen eighty-nine with the storming of Bastille.",
        "The Berlin Wall came down on November ninth nineteen eighty-nine ending Cold War.",
        "The signing of the Magna Carta in twelve fifteen limited royal authority in England.",
    ],
    "sports": [
        "A regulation soccer match consists of two halves of forty-five minutes each.",
        "The Tour de France bicycle race spans approximately three thousand five hundred km.",
        "Wimbledon is the oldest tennis tournament founded in eighteen seventy-seven.",
        "Sumo wrestling matches are held in a circular ring called a dohyo by tradition.",
        "The Stanley Cup is awarded annually to the National Hockey League playoff champion.",
        "A marathon race covers exactly twenty-six point two miles or 42.195 kilometers.",
        "The Olympic Games trace their origin to ancient Greek athletic competitions.",
        "Cricket originated in southeast England during the late sixteenth century.",
        "A grand slam in tennis means winning all four major tournaments in one calendar year.",
        "The Super Bowl is the championship game of the National Football League annually.",
    ],
    "plants": [
        "Photosynthesis converts carbon dioxide and water into glucose using sunlight energy.",
        "Bamboo is one of the fastest-growing plants in the world some species meters per day.",
        "The giant sequoia is the largest tree by volume on Earth native to California.",
        "Carnivorous plants like the Venus flytrap evolved in nutrient-poor soil environments.",
        "Tulip mania in seventeenth century Netherlands was an early speculative bubble.",
        "Mycorrhizal fungi form symbiotic networks with plant roots to exchange nutrients.",
        "Coffee beans are the seeds of cherries grown on trees in tropical highlands.",
        "The acorn from oak trees can take up to twenty-four months to fully develop.",
        "Bromeliads include the pineapple plant and many epiphytic tropical species.",
        "The lotus flower opens at dawn and closes at dusk in many Asian wetlands.",
    ],
    "weather": [
        "A rainbow forms when sunlight is refracted through suspended water droplets.",
        "Hurricanes are classified on the Saffir-Simpson scale based on sustained wind speed.",
        "Lightning strikes the Earth roughly one hundred times every second on average.",
        "The polar vortex is a persistent low-pressure system over the polar regions.",
        "Snowflakes are six-sided crystals formed by water vapor freezing in the atmosphere.",
        "Tornadoes form when warm moist air collides with cold dry air over flat terrain.",
        "El Nino warms surface waters in the equatorial Pacific affecting global weather.",
        "A monsoon is a seasonal wind reversal bringing heavy rain to South Asia in summer.",
        "Hailstones grow as ice pellets cycle up and down inside thunderstorm updrafts.",
        "The eye of a hurricane is calm because air sinks at the storm rotational center.",
    ],
    "transport": [
        "The Trans-Siberian Railway connects Moscow to Vladivostok over nine thousand km.",
        "Diesel engines were patented by Rudolf Diesel in eighteen ninety-two in Germany.",
        "The Suez Canal connects the Mediterranean Sea with the Red Sea through Egypt.",
        "Concorde was a supersonic passenger jet that operated commercially until 2003.",
        "Container shipping standardized cargo through twenty and forty foot ISO containers.",
        "The Panama Canal links the Atlantic and Pacific Oceans across Central America.",
        "High-speed rail in Japan known as Shinkansen began operations in nineteen sixty-four.",
        "The Boeing seven forty-seven was the first wide-body jumbo jet to enter service.",
        "The Tesla Model S was the first mass-market electric vehicle with luxury features.",
        "The London Underground opened in eighteen sixty-three as the first urban metro.",
    ],
}


def main() -> None:
    rows = []
    counter = 0
    for domain, facts in DOMAINS.items():
        for fact in facts:
            counter += 1
            is_orphan = counter % 3 == 0
            rows.append({
                "row_id": f"b{counter:03d}",
                "entity_type": "fact",
                "content": fact,
                "is_orphan_intent": is_orphan,
                "rationale": f"{domain} domain seed",
                "reviewed_by": "tim",
            })
    assert len(rows) == 100, f"expected 100 got {len(rows)}"
    out = Path("tests/fixtures/handlers/backfill_corpus.jsonl")
    out.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
