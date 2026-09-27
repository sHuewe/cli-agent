from __future__ import annotations

import ast
from decimal import (
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    localcontext,
)

MAX_EXPRESSION_CHARS = 512
MAX_AST_NODES = 128
MAX_AST_DEPTH = 32
MAX_POWER_EXPONENT = 1_000
MAX_RESULT_ADJUSTED_EXPONENT = 10_000
MAX_FUNCTION_ARGUMENTS = 32
MAX_ROUND_DIGITS = 100
DECIMAL_PRECISION = 50

_ALLOWED_BINARY_OPERATORS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
)
_ALLOWED_UNARY_OPERATORS = (ast.UAdd, ast.USub)
_ALLOWED_FUNCTIONS = frozenset({"abs", "round", "min", "max", "sqrt"})


class CalculatorError(ValueError):
    """A calculator expression is invalid, unsafe, or outside resource limits."""


def _format_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _validate_result(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise CalculatorError("Das Ergebnis muss eine endliche Dezimalzahl sein.")
    if not value.is_zero() and abs(value.adjusted()) > MAX_RESULT_ADJUSTED_EXPONENT:
        raise CalculatorError(
            "Das Ergebnis überschreitet die zulässige Größenordnung."
        )
    return value


class _DecimalEvaluator:
    def __init__(self, expression: str) -> None:
        self.expression = expression

    def evaluate(self, node: ast.AST, *, depth: int = 0) -> Decimal:
        if depth > MAX_AST_DEPTH:
            raise CalculatorError("Der Rechenausdruck ist zu tief verschachtelt.")

        if isinstance(node, ast.Expression):
            return self.evaluate(node.body, depth=depth + 1)

        if isinstance(node, ast.Constant):
            return self._number(node)

        if isinstance(node, ast.UnaryOp) and isinstance(
            node.op,
            _ALLOWED_UNARY_OPERATORS,
        ):
            operand = self.evaluate(node.operand, depth=depth + 1)
            result = operand if isinstance(node.op, ast.UAdd) else -operand
            return _validate_result(result)

        if isinstance(node, ast.BinOp) and isinstance(
            node.op,
            _ALLOWED_BINARY_OPERATORS,
        ):
            return self._binary(node, depth=depth)

        if isinstance(node, ast.Call):
            return self._call(node, depth=depth)

        raise CalculatorError(
            "Nicht unterstützter Ausdruck. Erlaubt sind Zahlen, Klammern, "
            "+, -, *, /, %, ** sowie abs(), round(), min(), max() und sqrt()."
        )

    def _number(self, node: ast.Constant) -> Decimal:
        if type(node.value) not in {int, float}:
            raise CalculatorError("Nur numerische Literale sind erlaubt.")
        literal = ast.get_source_segment(self.expression, node)
        if literal is None:
            raise CalculatorError("Numerisches Literal konnte nicht gelesen werden.")
        try:
            value = Decimal(literal.replace("_", ""))
        except DecimalException as exc:
            raise CalculatorError(
                f"Ungültiges Dezimalliteral: {literal!r}"
            ) from exc
        return _validate_result(value)

    def _binary(self, node: ast.BinOp, *, depth: int) -> Decimal:
        left = self.evaluate(node.left, depth=depth + 1)
        right = self.evaluate(node.right, depth=depth + 1)
        try:
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                result = left / right
            elif isinstance(node.op, ast.Mod):
                result = left % right
            else:
                result = self._power(left, right)
        except (DivisionByZero, InvalidOperation, DecimalException, ZeroDivisionError) as exc:
            raise CalculatorError(f"Rechenoperation ist nicht definiert: {exc}") from exc
        return _validate_result(result)

    @staticmethod
    def _power(base: Decimal, exponent: Decimal) -> Decimal:
        integral = exponent.to_integral_value()
        if exponent != integral:
            raise CalculatorError(
                "Der Exponent von ** muss eine ganze Zahl sein; "
                "für Quadratwurzeln steht sqrt() zur Verfügung."
            )
        exponent_int = int(integral)
        if abs(exponent_int) > MAX_POWER_EXPONENT:
            raise CalculatorError(
                f"Der Betrag des Exponenten darf höchstens "
                f"{MAX_POWER_EXPONENT} sein."
            )
        try:
            return base ** exponent_int
        except (DivisionByZero, InvalidOperation, DecimalException, ZeroDivisionError) as exc:
            raise CalculatorError(f"Potenz ist nicht definiert: {exc}") from exc

    def _call(self, node: ast.Call, *, depth: int) -> Decimal:
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id not in _ALLOWED_FUNCTIONS
        ):
            raise CalculatorError(
                "Nur abs(), round(), min(), max() und sqrt() sind erlaubt."
            )
        if node.keywords:
            raise CalculatorError("Calculator-Funktionen erlauben keine Keyword-Argumente.")
        if len(node.args) > MAX_FUNCTION_ARGUMENTS:
            raise CalculatorError(
                f"Eine Funktion darf höchstens {MAX_FUNCTION_ARGUMENTS} Argumente erhalten."
            )

        name = node.func.id
        values = [
            self.evaluate(argument, depth=depth + 1)
            for argument in node.args
        ]

        if name == "abs":
            self._require_arity(name, values, 1)
            return _validate_result(abs(values[0]))

        if name == "sqrt":
            self._require_arity(name, values, 1)
            if values[0] < 0:
                raise CalculatorError("sqrt() ist für negative Werte nicht definiert.")
            try:
                return _validate_result(values[0].sqrt())
            except (InvalidOperation, DecimalException) as exc:
                raise CalculatorError(f"sqrt() konnte nicht berechnet werden: {exc}") from exc

        if name == "round":
            if len(values) not in {1, 2}:
                raise CalculatorError("round() erwartet ein oder zwei Argumente.")
            if len(values) == 1:
                return _validate_result(
                    values[0].to_integral_value()
                )
            digits = values[1].to_integral_value()
            if values[1] != digits:
                raise CalculatorError(
                    "Das zweite Argument von round() muss eine ganze Zahl sein."
                )
            digits_int = int(digits)
            if abs(digits_int) > MAX_ROUND_DIGITS:
                raise CalculatorError(
                    f"round() erlaubt höchstens {MAX_ROUND_DIGITS} "
                    "Nachkommastellen bzw. Stellen links vom Komma."
                )
            try:
                return _validate_result(round(values[0], digits_int))
            except (InvalidOperation, DecimalException) as exc:
                raise CalculatorError(f"round() konnte nicht berechnet werden: {exc}") from exc

        if not values:
            raise CalculatorError(f"{name}() erwartet mindestens ein Argument.")
        return _validate_result(
            min(values) if name == "min" else max(values)
        )

    @staticmethod
    def _require_arity(name: str, values: list[Decimal], count: int) -> None:
        if len(values) != count:
            raise CalculatorError(
                f"{name}() erwartet genau {count} Argument"
                + ("" if count == 1 else "e")
                + "."
            )


def calculate_expression(expression: str) -> str:
    if not isinstance(expression, str) or not expression.strip():
        raise CalculatorError("Rechenausdruck darf nicht leer sein.")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise CalculatorError(
            f"Rechenausdruck darf höchstens {MAX_EXPRESSION_CHARS} Zeichen lang sein."
        )

    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise CalculatorError("Ungültiger Rechenausdruck.") from exc

    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise CalculatorError(
            f"Rechenausdruck darf höchstens {MAX_AST_NODES} Syntaxknoten enthalten."
        )

    evaluator = _DecimalEvaluator(expression)
    try:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            result = evaluator.evaluate(tree)
    except CalculatorError:
        raise
    except (DecimalException, ArithmeticError, ValueError, OverflowError) as exc:
        raise CalculatorError(f"Rechenausdruck konnte nicht berechnet werden: {exc}") from exc

    return _format_decimal(result)
