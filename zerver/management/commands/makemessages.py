"""
See https://zulip.readthedocs.io/en/latest/translating/internationalization.html
for background.

The contents of this file are taken from
https://github.com/niwinz/django-jinja/blob/master/django_jinja/management/commands/makemessages.py

Jinja2's i18n functionality is not exactly the same as Django's.
In particular, the tags names and their syntax are different:

  1. The Django ``trans`` tag is replaced by a _() global.
  2. The Django ``blocktrans`` tag is called ``trans``.

(1) isn't an issue, since the whole ``makemessages`` process is based on
converting the template tags to ``_()`` calls. However, (2) means that
those Jinja2 ``trans`` tags will not be picked up by Django's
``makemessages`` command.

There aren't any nice solutions here. While Jinja2's i18n extension does
come with extraction capabilities built in, the code behind ``makemessages``
unfortunately isn't extensible, so we can:

  * Duplicate the command + code behind it.
  * Offer a separate command for Jinja2 extraction.
  * Try to get Django to offer hooks into makemessages().
  * Monkey-patch.

We are currently doing that last thing. It turns out there we are lucky
for once: It's simply a matter of extending two regular expressions.
Credit for the approach goes to:
https://stackoverflow.com/questions/2090717

"""

import glob
import os
import re
import subprocess
from collections.abc import Collection, Iterator, Mapping
from typing import Any

import orjson
from django.core.management.base import CommandParser
from django.core.management.commands import makemessages
from django.template import engines
from django.template.backends.jinja2 import Jinja2
from django.template.base import BLOCK_TAG_END, BLOCK_TAG_START
from django.utils.translation import template
from jinja2.environment import Environment
from typing_extensions import override

strip_whitespace_right = re.compile(
    rf"({BLOCK_TAG_START}-?\s*(trans|pluralize).*?-{BLOCK_TAG_END})\s+"
)
strip_whitespace_left = re.compile(
    rf"\s+({BLOCK_TAG_START}-\s*(endtrans|pluralize).*?-?{BLOCK_TAG_END})"
)
trim_blocks = re.compile(rf"({BLOCK_TAG_START}[-+]?\s*(trans|pluralize)[^+]*?{BLOCK_TAG_END})\n")
lstrip_blocks = re.compile(
    rf"^[ \t]+({BLOCK_TAG_START}\s*(endtrans|pluralize).*?[-+]?{BLOCK_TAG_END})",
    re.MULTILINE,
)

regexes = [
    r"{{~?#tr}}([\s\S]*?)(?:~?{{/tr}}|{{~?#\*inline )",  # '.' doesn't match '\n' by default
    r'{{~?\s*t "([\s\S]*?)"\W*~?}}',
    r"{{~?\s*t '([\s\S]*?)'\W*~?}}",
    r'\(t "([\s\S]*?)"\)',
    r'=\(t "([\s\S]*?)"\)(?=[^{]*}})',
    r"=\(t '([\s\S]*?)'\)(?=[^{]*}})",
]
tags = [
    ("err_", "error"),
]

frontend_compiled_regexes = [re.compile(regex) for regex in regexes]


def strip_whitespaces(src: str, env: Environment) -> str:
    src = strip_whitespace_left.sub(r"\1", src)
    src = strip_whitespace_right.sub(r"\1", src)
    if env.trim_blocks:
        src = trim_blocks.sub(r"\1", src)
    if env.lstrip_blocks:
        src = lstrip_blocks.sub(r"\1", src)
    return src


# this regex looks for {% trans %} blocks that don't have 'trimmed' or 'notrimmed' set.
# capturing {% endtrans %} ensures this doesn't affect DTL {% trans %} tags.
trans_block_re = re.compile(
    rf"({BLOCK_TAG_START}[-+]?\s*trans)(?!\s+(?:no)?trimmed)"
    rf"(.*?{BLOCK_TAG_END}.*?{BLOCK_TAG_START}[-+]?\s*?endtrans\s*?[-+]?{BLOCK_TAG_END})",
    re.DOTALL,
)


def apply_i18n_trimmed_policy(src: str, env: Environment) -> str:
    # if env.policies["ext.i18n.trimmed"]: insert 'trimmed' flag on jinja {% trans %} blocks.
    if not env.policies.get("ext.i18n.trimmed", False):
        return src
    return trans_block_re.sub(r"\1 trimmed \2", src)


class Command(makemessages.Command):
    xgettext_options = makemessages.Command.xgettext_options
    for func, tag in tags:
        xgettext_options += [f'--keyword={func}:1,"{tag}"']

    @override
    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--frontend-source",
            default="web/templates",
            help="Name of the Handlebars template directory",
        )
        parser.add_argument(
            "--frontend-output",
            default="locale",
            help="Name of the frontend messages output directory",
        )
        parser.add_argument(
            "--frontend-namespace",
            default="translations.json",
            help="Namespace of the frontend locale file",
        )

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        self.handle_django_locales(*args, **options)
        self.handle_frontend_locales(**options)

    def handle_frontend_locales(
        self,
        *,
        frontend_source: str,
        frontend_output: str,
        frontend_namespace: str,
        locale: list[str],
        exclude: list[str],
        all: bool,
        **options: Any,
    ) -> None:
        self.frontend_source = frontend_source
        self.frontend_output = frontend_output
        self.frontend_namespace = frontend_namespace
        self.frontend_locale = locale
        self.frontend_exclude = exclude
        self.frontend_all = all

        translation_strings = self.get_translation_strings()
        self.write_translation_strings(translation_strings)

    def handle_django_locales(self, *args: Any, **options: Any) -> None:
        old_endblock_re = template.endblock_re
        old_block_re = template.block_re
        old_constant_re = template.constant_re

        old_templatize = template.templatize
        # Extend the regular expressions that are used to detect
        # translation blocks with an "OR jinja-syntax" clause.
        template.endblock_re = re.compile(
            template.endblock_re.pattern + "|" + r"""^[-+]?\s*endtrans\s*[-+]?$"""
        )
        template.block_re = re.compile(
            template.block_re.pattern
            + "|"
            + r"""^[-+]?\s*trans(?:\s+(?:no)?trimmed)?(?:\s+(?!'|")(?=.*?=.*?)|\s*[-+]?$)"""
        )
        template.plural_re = re.compile(
            template.plural_re.pattern + "|" + r"""^[-+]?\s*pluralize(?:\s+.+|[-+]?$)"""
        )
        template.constant_re = re.compile(r""".*?_\(((?:".*?(?<!\\)")|(?:'.*?(?<!\\)')).*?\)""")

        jinja_engine = engines["Jinja2"]
        assert isinstance(jinja_engine, Jinja2)

        def my_templatize(src: str, *args: Any, **kwargs: Any) -> str:
            new_src = strip_whitespaces(src, jinja_engine.env)
            new_src = apply_i18n_trimmed_policy(new_src, jinja_engine.env)
            return old_templatize(new_src, *args, **kwargs)

        template.templatize = my_templatize

        try:
            ignore_patterns = options.get("ignore_patterns", [])
            ignore_patterns.append("docs/*")
            ignore_patterns.append("templates/zerver/emails/custom/*")
            ignore_patterns.append("var/*")
            options["ignore_patterns"] = ignore_patterns
            super().handle(*args, **options)
        finally:
            template.endblock_re = old_endblock_re
            template.block_re = old_block_re
            template.templatize = old_templatize
            template.constant_re = old_constant_re

    def extract_strings(self, data: str) -> list[str]:
        translation_strings: list[str] = []
        for regex in frontend_compiled_regexes:
            for match in regex.findall(data):
                match = match.strip()
                match = " ".join(line.strip() for line in match.splitlines())
                translation_strings.append(match)

        return translation_strings

    def get_translation_strings(self) -> list[str]:
        translation_strings: list[str] = []
        dirname = self.get_template_dir()

        for dirpath, dirnames, filenames in os.walk(dirname):
            for filename in [f for f in filenames if f.endswith(".hbs")]:
                if filename.startswith("."):
                    continue
                with open(os.path.join(dirpath, filename)) as reader:
                    data = reader.read()
                    translation_strings.extend(self.extract_strings(data))

        extracted = subprocess.check_output(
            [
                "node_modules/.bin/formatjs",
                "extract",
                "--additional-function-names=$t,$t_html",
                "--format=simple",
                "--ignore=**/*.d.ts",
                "web/src/**/*.js",
                "web/src/**/*.ts",
            ]
        )
        translation_strings.extend(orjson.loads(extracted).values())

        return list(set(translation_strings))

    def get_template_dir(self) -> str:
        return self.frontend_source

    def get_namespace(self) -> str:
        return self.frontend_namespace

    def get_locales(self) -> Collection[str]:
        locale = self.frontend_locale
        exclude = self.frontend_exclude
        process_all = self.frontend_all

        # After calling super().handle(), default_locale_path gets set on self
        # so that we can reuse it here.
        default_locale_path = self.default_locale_path
        paths = glob.glob(f"{default_locale_path}/*")
        all_locales = [os.path.basename(path) for path in paths if os.path.isdir(path)]

        # Account for excluded locales
        if process_all:
            return all_locales
        else:
            locales = locale or all_locales
            return set(locales) - set(exclude)

    def get_base_path(self) -> str:
        return self.frontend_output

    def get_output_paths(self) -> Iterator[str]:
        base_path = self.get_base_path()
        locales = self.get_locales()
        for path in [os.path.join(base_path, locale) for locale in locales]:
            if not os.path.exists(path):
                os.makedirs(path)

            yield os.path.join(path, self.get_namespace())

    def get_new_strings(
        self, old_strings: Mapping[str, str], translation_strings: list[str], locale: str
    ) -> dict[str, str]:
        """
        Missing strings are removed, new strings are added and already
        translated strings are not touched.
        """
        new_strings = {}  # Dict[str, str]
        for k in translation_strings:
            if locale == "en":
                # For English language, translation is equal to the key.
                new_strings[k] = old_strings.get(k, k)
            else:
                new_strings[k] = old_strings.get(k, "")

        return new_strings

    def write_translation_strings(self, translation_strings: list[str]) -> None:
        for locale, output_path in zip(self.get_locales(), self.get_output_paths(), strict=False):
            self.stdout.write(f"[frontend] processing locale {locale}")
            try:
                with open(output_path, "rb") as reader:
                    old_strings = orjson.loads(reader.read())
            except (OSError, ValueError):
                old_strings = {}

            new_strings = self.get_new_strings(old_strings, translation_strings, locale)
            with open(output_path, "wb") as writer:
                writer.write(
                    orjson.dumps(
                        new_strings,
                        option=orjson.OPT_APPEND_NEWLINE
                        | orjson.OPT_INDENT_2
                        | orjson.OPT_SORT_KEYS,
                    )
                )
