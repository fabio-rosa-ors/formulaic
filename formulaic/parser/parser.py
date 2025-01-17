import ast
import itertools
import functools
import re
from typing import Any, Iterable, Union

from formulaic.errors import FormulaParsingError

from .algos.tokens_to_ast import tokens_to_ast
from .algos.tokenize import tokenize
from .types import ASTNode, Factor, Operator, OperatorResolver, Term, Token
from .utils import exc_for_token


class FormulaParser:
    """
    The default and base formula parser for `Formula`s.

    The role of this class is to transform a string representation of a formula
    to a sequence of `Term` instances that can be evaluated by materializers
    and ultimately rendered into model matrices.

    This class can be subclassed to customize this behavior. The three phases of
    formula parsing are split out into separate methods to make this easier.
    They are:
        - get_tokens: Which returns an iterable of `Token` instances. By default
            this uses `tokenize()` and handles the addition/removal of the
            intercept.
        - get_ast: Which converts the iterable of `Token`s into an abstract
            syntax tree. By default this uses `tokens_to_ast()` and the nominated
            `OperatorResolver` instance.
        - get_terms: Which evaluates the abstract syntax tree and returns an
            iterable of `Term`s.
    Only the `get_terms()` method is essential from an API perspective.

    Attributes:
        operator_resolver: The operator resolver to use when parsing the formula
            string and generating the abstract syntax tree. If not specified,
            it will default to `DefaultOperatorResolver`.
    """

    def __init__(self, operator_resolver: OperatorResolver = None):
        self.operator_resolver = operator_resolver or DefaultOperatorResolver()

    def get_tokens(
        self, formula: str, *, include_intercept: bool = True
    ) -> Iterable[Token]:
        """
        Return an iterable of `Token` instances for the nominated `formula`
        string.

        Args:
            formula: The formula string to be tokenized.
            include_intercept: Whether to include an intercept by default
                (formulas can still omit this intercept in the usual manner:
                adding a '-1' or '+0' term).
        """
        tokens = list(tokenize(formula))

        # Insert "1" or "1 + " to beginning of RHS formula
        one = Token(token="1", kind="value")
        plus = Token(token="+", kind="operator")
        minus = Token(token="-", kind="operator")

        if include_intercept:
            if len(tokens) == 0:
                tokens = [one]
            else:
                try:
                    tilde_index = len(tokens) - 1 - tokens[::-1].index("~")
                    if tilde_index == len(tokens) - 1:
                        tokens.append(one)
                    else:
                        tokens.insert(tilde_index + 1, one)
                        tokens.insert(tilde_index + 2, plus)
                except ValueError:
                    tokens.insert(0, one)
                    tokens.insert(1, plus)

        # Replace all "0"s with "-1"
        zero_index = -1
        try:
            while True:
                zero_index = tokens.index("0", zero_index + 1)
                if zero_index - 1 < 0 or tokens[zero_index - 1] == "~":
                    tokens.pop(zero_index)
                    zero_index -= 1
                    continue
                elif tokens[zero_index - 1] == "+":
                    tokens[zero_index - 1] = minus
                elif tokens[zero_index - 1] == "-":
                    tokens[zero_index - 1] = plus
                else:
                    raise FormulaParsingError(
                        f"Unrecognised use of `0` at index: {tokens[zero_index-1].source_start}."
                    )
                tokens[zero_index] = one
        except ValueError:
            pass

        return tokens

    def get_ast(self, formula: str, *, include_intercept: bool = True) -> ASTNode:
        """
        Assemble an abstract syntax tree for the nominated `formula` string.

        Args:
            formula: The formula for which an AST should be generated.
            include_intercept: Whether to include an intercept by default
                (formulas can still omit this intercept in the usual manner:
                adding a '-1' or '+0' term).
        """
        return tokens_to_ast(
            self.get_tokens(formula, include_intercept=include_intercept),
            operator_resolver=self.operator_resolver,
        )

    def get_terms(
        self, formula, *, sort=True, include_intercept=True
    ) -> Union[Iterable[Term], Iterable[Iterable[Term]], Any]:
        """
        Assemble the `Term` instances for a formula string. Depending on the
        operators involved, this may be an iterable of `Term` instances, or
        an iterable of iterables of `Term`s, etc.

        Args:
            formula: The formula for which an AST should be generated.
            sort: Whether to sort the terms before returning them.
            include_intercept: Whether to include an intercept by default
                (formulas can still omit this intercept in the usual manner:
                adding a '-1' or '+0' term).
        """
        terms = self.get_ast(formula, include_intercept=include_intercept).to_terms()

        if sort:
            if isinstance(terms, tuple):
                terms = tuple(sorted(ts) for ts in terms)
            else:
                terms = sorted(terms)

        return terms


class DefaultOperatorResolver(OperatorResolver):
    """
    The default operator resolver implementation.

    This class implements the standard operators in a form consistent with
    other implementations of Wilkinson formulas. It can be extended via
    subclassing to support other kinds of operators, in which case `.operators`
    and/or `.resolve` can be overridden. For more details about which operators
    are implemented, review the code or the documentation website.
    """

    @property
    def operators(self):
        def formula_separator_expansion(lhs, rhs):
            terms = (lhs.to_terms(), rhs.to_terms())

            out = []
            for termset in terms:
                if isinstance(termset, tuple):
                    out.extend(termset)
                else:
                    out.append(termset)
            return tuple(out)

        def nested_product_expansion(parents, nested):
            terms = parents.to_terms()
            common = functools.reduce(lambda x, y: x * y, terms)
            return terms.union({common * term for term in nested.to_terms()})

        def unary_negation(arg):
            # TODO: FormulaParser().get_terms('a * ( - b)') Should return `a`
            terms = arg.to_terms()
            if len(terms) > 1 or list(terms)[0] != "0":
                raise FormulaParsingError(
                    "Unary negation is only implemented for '0', where it is substituted for '1'."
                )
            return {
                Term(factors=[Factor("1", eval_method="literal")])
            }  # pragma: no cover; All zero handling is currently done in the token pre-processor.

        def power(arg, power):
            if (
                not isinstance(power, Token)
                or power.kind is not Token.Kind.VALUE
                or not isinstance(ast.literal_eval(power.token), int)
            ):
                raise exc_for_token(
                    power, "The right-hand argument of `**` must be a positive integer."
                )
            return {
                functools.reduce(lambda x, y: x * y, term)
                for term in itertools.product(*[arg.to_terms()] * int(power.token))
            }

        return [
            Operator(
                "~",
                arity=2,
                precedence=-100,
                associativity=None,
                to_terms=formula_separator_expansion,
            ),
            Operator(
                "~",
                arity=1,
                precedence=-100,
                associativity=None,
                fixity="prefix",
                to_terms=lambda expr: (expr.to_terms(),),
            ),
            Operator(
                "+",
                arity=2,
                precedence=100,
                associativity="left",
                to_terms=lambda *args: set(
                    itertools.chain(*[arg.to_terms() for arg in args])
                ),
            ),
            Operator(
                "-",
                arity=2,
                precedence=100,
                associativity="left",
                to_terms=lambda left, right: set(
                    set(left.to_terms()).difference(right.to_terms())
                ),
            ),
            Operator(
                "+",
                arity=1,
                precedence=100,
                associativity="right",
                fixity="prefix",
                to_terms=lambda arg: arg.to_terms(),
            ),
            Operator(
                "-",
                arity=1,
                precedence=100,
                associativity="right",
                fixity="prefix",
                to_terms=unary_negation,
            ),
            Operator(
                "*",
                arity=2,
                precedence=200,
                associativity="left",
                to_terms=lambda *args: (
                    {
                        functools.reduce(lambda x, y: x * y, term)
                        for term in itertools.product(*[arg.to_terms() for arg in args])
                    }.union(itertools.chain(*[arg.to_terms() for arg in args]))
                ),
            ),
            Operator(
                "/",
                arity=2,
                precedence=200,
                associativity="left",
                to_terms=nested_product_expansion,
            ),
            Operator(
                ":",
                arity=2,
                precedence=300,
                associativity="left",
                to_terms=lambda *args: {
                    functools.reduce(lambda x, y: x * y, term)
                    for term in itertools.product(*[arg.to_terms() for arg in args])
                },
            ),
            Operator(
                "**", arity=2, precedence=500, associativity="right", to_terms=power
            ),
        ]

    def resolve(self, token: Token, max_prefix_arity) -> Iterable[Operator]:
        if token.token in self.operator_table:
            return super().resolve(token, max_prefix_arity)

        symbol = token.token

        # Apply R-like transformations to operator
        symbol = re.sub(
            r"[+\-]*\-[+\-]*", "-", symbol
        )  # Any sequence of '+' and '-' -> '-'
        symbol = re.sub(r"[+]{2,}", "+", symbol)  # multiple sequential '+' -> '+'

        if symbol in self.operator_table:
            return [self._resolve(token, symbol, max_prefix_arity)]

        return [
            self._resolve(token, sym, max_prefix_arity if i == 0 else 0)
            for i, sym in enumerate(symbol)
        ]
