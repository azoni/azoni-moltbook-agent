"""
Verification challenge solver for Moltbook API v1.12.0.

Moltbook sends obfuscated math problems that must be solved to verify
posts and comments. The text uses alternating caps, scattered symbols,
and broken words to obfuscate a simple word problem.

Pipeline: deobfuscate -> parse word problem -> solve -> format answer
"""
import re
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Word-to-number mapping
WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "thousand": 1000,
}

# Common misspellings/obfuscation artifacts (doubled/mangled letters)
WORD_CORRECTIONS = {
    "twentyy": "twenty", "twenntyy": "twenty", "twenny": "twenty",
    "thirtyy": "thirty", "fortyy": "forty", "fiftyy": "fifty",
    "sixtyy": "sixty", "seventyy": "seventy", "eightyy": "eighty",
    "ninetyy": "ninety",
    "fivee": "five", "threee": "three", "onee": "one",
    "fourr": "four", "sixx": "six", "sevenn": "seven",
    "eightt": "eight", "ninee": "nine", "tenn": "ten",
    "elevenn": "eleven", "twelvee": "twelve",
}

# Operation keyword mapping
ADD_KEYWORDS = {
    "plus", "adds", "add", "gains", "gain", "increases", "increase",
    "grows", "grow", "gets", "get", "earns", "earn", "receives",
    "receive", "finds", "find", "picks", "pick", "collects", "collect",
    "more", "additional", "extra", "joined", "combined", "together",
}
SUB_KEYWORDS = {
    "minus", "loses", "lose", "slows", "slow", "decreases", "decrease",
    "drops", "drop", "falls", "fall", "subtracts", "subtract",
    "less", "reduces", "reduce", "gives", "give", "spends", "spend",
    "removes", "remove", "away", "lost", "fewer",
}
MUL_KEYWORDS = {
    "times", "multiplied", "multiplies", "multiply", "doubled", "tripled",
    "groups", "of", "product",
}
DIV_KEYWORDS = {
    "divided", "divides", "divide", "splits", "split", "halved",
    "shared", "ratio", "per",
}


def deobfuscate_challenge(text: str) -> str:
    """
    Strip non-alpha chars (except spaces), normalize case, clean up.

    Example: "A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS aNd] SlO/wS bY^ fI[vE"
    -> "a lobster swims at twenty meters and slows by five"
    """
    # Remove all non-alpha characters except spaces
    cleaned = re.sub(r'[^a-zA-Z\s]', '', text)
    # Normalize whitespace and lowercase
    cleaned = ' '.join(cleaned.lower().split())
    # Apply word corrections for obfuscation artifacts
    words = cleaned.split()
    corrected = []
    for word in words:
        corrected.append(WORD_CORRECTIONS.get(word, word))
    return ' '.join(corrected)


def _parse_compound_number(words: list, start_idx: int) -> Tuple[Optional[float], int]:
    """
    Parse compound numbers like 'twenty five' (= 25) or 'three hundred'.
    Returns (number, next_index) or (None, start_idx).
    """
    if start_idx >= len(words):
        return None, start_idx

    word = words[start_idx]

    # Try direct numeric
    try:
        return float(word), start_idx + 1
    except ValueError:
        pass

    # Try word-to-number
    if word not in WORD_NUMBERS:
        return None, start_idx

    val = WORD_NUMBERS[word]
    idx = start_idx + 1

    # Check for "hundred" multiplier: "three hundred" -> 300
    if idx < len(words) and words[idx] == "hundred":
        val *= 100
        idx += 1
        # Check for additional: "three hundred twenty five"
        if idx < len(words) and words[idx] in WORD_NUMBERS:
            tens = WORD_NUMBERS[words[idx]]
            if tens >= 20:
                val += tens
                idx += 1
                if idx < len(words) and words[idx] in WORD_NUMBERS and WORD_NUMBERS[words[idx]] < 10:
                    val += WORD_NUMBERS[words[idx]]
                    idx += 1
            elif tens < 20:
                val += tens
                idx += 1

    # Check for compound: "twenty five" -> 25
    elif val >= 20 and val < 100 and idx < len(words):
        next_word = words[idx]
        if next_word in WORD_NUMBERS and WORD_NUMBERS[next_word] < 10:
            val += WORD_NUMBERS[next_word]
            idx += 1

    return float(val), idx


def parse_word_problem(text: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    """
    Extract two numbers and one operation from deobfuscated text.

    Returns: (number1, operation_char, number2) or (None, None, None)
    """
    words = text.split()

    numbers = []
    operation = None

    i = 0
    while i < len(words):
        word = words[i]

        # Check for operation keywords (only capture the first one)
        if operation is None:
            if word in ADD_KEYWORDS:
                operation = "+"
                i += 1
                continue
            elif word in SUB_KEYWORDS:
                operation = "-"
                i += 1
                continue
            elif word in MUL_KEYWORDS:
                operation = "*"
                i += 1
                continue
            elif word in DIV_KEYWORDS:
                operation = "/"
                i += 1
                continue

        # Try to parse a number
        num, next_i = _parse_compound_number(words, i)
        if num is not None:
            numbers.append(num)
            i = next_i
            continue

        i += 1

    if len(numbers) >= 2 and operation:
        return numbers[0], operation, numbers[1]
    elif len(numbers) >= 2:
        # Default to addition if no operation found
        logger.warning(f"No operation keyword found in '{text}', defaulting to +")
        return numbers[0], "+", numbers[1]

    return None, None, None


def solve_challenge(challenge_text: str) -> Optional[str]:
    """
    Full pipeline: deobfuscate -> parse -> solve -> format.

    Returns answer as string with 2 decimal places, e.g. "15.00",
    or None if unable to solve.
    """
    clean_text = deobfuscate_challenge(challenge_text)
    logger.info(f"[verification] Deobfuscated: '{clean_text}'")

    num1, op, num2 = parse_word_problem(clean_text)

    if num1 is None or op is None or num2 is None:
        logger.warning(f"[verification] Could not parse word problem from: '{clean_text}'")
        return None

    logger.info(f"[verification] Parsed: {num1} {op} {num2}")

    if op == "+":
        result = num1 + num2
    elif op == "-":
        result = num1 - num2
    elif op == "*":
        result = num1 * num2
    elif op == "/":
        if num2 == 0:
            logger.error("[verification] Division by zero")
            return None
        result = num1 / num2
    else:
        return None

    answer = f"{result:.2f}"
    logger.info(f"[verification] Answer: {answer}")
    return answer
