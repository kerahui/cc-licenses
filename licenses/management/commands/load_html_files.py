import gettext
import os
import sys
from argparse import ArgumentParser

from bs4 import BeautifulSoup, Tag
from django.core.management import BaseCommand
from django.utils.translation import to_language, to_locale
from polib import POFile, POEntry

from i18n import DEFAULT_LANGUAGE_CODE
from licenses.bs_utils import inner_html, name_and_text, text_up_to, nested_text
from licenses.models import License, LegalCode
from licenses.utils import (
    validate_dictionary_is_all_text,
    parse_legalcode_filename,
)


class Command(BaseCommand):
    """
    Read the HTML files from a directory, figure out which licenses they are, and create
    and populate the corresponding License and LegalCode objects.  Then parse the HTML
    and create or update the .po and .mo files.
    """

    def add_arguments(self, parser: ArgumentParser):
        parser.add_argument("input_directory")

    def handle(self, input_directory, **options):
        # We're just doing the BY 4.0 licenses for now
        licenses_created = 0
        legalcodes_created = 0

        # We'll create LegalCode and License objects for all the by* HTML files.
        # (We're only going to parse the HTML for the 4.0 ones for now, though.)
        html_filenames = sorted(
            [
                f
                for f in os.listdir(input_directory)
                if f.startswith("by") and f.endswith(".html")
            ]
        )
        for filename in html_filenames:
            metadata = parse_legalcode_filename(filename)

            basename = os.path.splitext(filename)[0]
            fullpath = os.path.join(input_directory, filename)

            license_code = metadata["license_code"]
            version = metadata["version"]
            jurisdiction_code = metadata["jurisdiction_code"]
            language_code = metadata["language_code"] or DEFAULT_LANGUAGE_CODE
            about_url = metadata["about_url"]

            # These are valid for BY only
            license_code_parts = license_code.split("-")
            if "by" in license_code_parts:
                permits_derivative_works = "nd" not in license_code_parts
                permits_reproduction = "nd" not in license_code_parts
                permits_distribution = "nd" not in license_code_parts
                permits_sharing = "nd" not in license_code_parts
                requires_share_alike = "sa" in license_code_parts
                requires_notice = True
                requires_attribution = True
                requires_source_code = False  # GPL, LGPL only, I think
                prohibits_commercial_use = "nc" in license_code_parts
                prohibits_high_income_nation_use = False  # Not any BY 4.0 license
            else:
                raise NotImplementedError(basename)

            # Find or create a License object
            license, created = License.objects.get_or_create(
                about=about_url,
                defaults=dict(
                    license_code=license_code,
                    version=version,
                    jurisdiction_code=jurisdiction_code,
                    permits_derivative_works=permits_derivative_works,
                    permits_reproduction=permits_reproduction,
                    permits_distribution=permits_distribution,
                    permits_sharing=permits_sharing,
                    requires_share_alike=requires_share_alike,
                    requires_notice=requires_notice,
                    requires_attribution=requires_attribution,
                    requires_source_code=requires_source_code,
                    prohibits_commercial_use=prohibits_commercial_use,
                    prohibits_high_income_nation_use=prohibits_high_income_nation_use,
                ),
            )
            if created:
                licenses_created += 1
            # Find or create a LegalCode object
            legalcode, created = LegalCode.objects.get_or_create(
                license=license,
                language_code=language_code,
                defaults=dict(html_file=fullpath,),
            )

            if created:
                legalcodes_created += 1
        print(
            f"Created {licenses_created} licenses and {legalcodes_created} translation objects"
        )

        # NOW parse the HTML and output message files

        # We're just doing these license codes and version 4.0 for now.
        LICENSE_CODES = ["by", "by-sa", "by-nc-nd", "by-nc", "by-nc-sa", "by-nd"]
        version = "4.0"

        # What are the language codes we have translations for?
        language_codes = list(
            LegalCode.objects.filter(
                license__version=version, license__license_code__startswith="by"
            )
            .order_by("language_code")
            .distinct("language_code")
            .values_list("language_code", flat=True)
        )

        english_by_license_code = {}

        # We have to do English first. Django gets confused if you try to load
        # another language and it can't find English, I guess it's looking for
        # something to fall back to.
        language_codes.remove("en")
        for language_code in ["en"] + language_codes:
            for license_code in LICENSE_CODES:
                legalcode = LegalCode.objects.get(
                    license__license_code=license_code,
                    license__version=version,
                    language_code=language_code,
                )

                print(legalcode.html_file)
                with open(legalcode.html_file, "r", encoding="utf-8") as f:
                    content = f.read()

                messages_text = self.import_by_40_license_html(
                    content, license_code, language_code
                )

                if language_code == "en":
                    english_by_license_code[license_code] = messages_text

                english_text = english_by_license_code[license_code]

                # Save to a .po file for this language.
                domain = legalcode.license.translation_domain

                # Use fake language of language+domain to switch to this set of translations
                punctuationless_language_code = (
                    language_code.replace("-", "").replace("_", "").replace(".", "")
                )
                lang_plus_domain = to_language(
                    f"{punctuationless_language_code}{domain}"
                )

                pofile = POFile()
                pofile.metadata = {
                    "Project-Id-Version": domain,
                    # 'Report-Msgid-Bugs-To': 'you@example.com',
                    # 'POT-Creation-Date': '2007-10-18 14:00+0100',
                    # 'PO-Revision-Date': '2007-10-18 14:00+0100',
                    # 'Last-Translator': 'you <you@example.com>',
                    # 'Language-Team': 'English <yourteam@example.com>',
                    "Language": language_code,
                    "MIME-Version": "1.0",
                    "Content-Type": "text/plain; charset=utf-8",
                    "Content-Transfer-Encoding": "8bit",
                }

                for internal_key, translation in messages_text.items():
                    # Always use english text as the translation key (if we have it)
                    if internal_key in english_text:
                        message_key = english_text[internal_key]
                    else:
                        message_key = translation  # PUNT.
                    # For English file, translations are empty.
                    message_value = "" if language_code == "en" else translation
                    pofile.append(
                        POEntry(msgid=message_key, msgstr=message_value.strip())
                    )

                assert "license_medium" in messages_text

                # Dir name will be like "en_US" or "tr_TR" or "zh_CN" or "zh-Hans"

                django_language_code = to_language(lang_plus_domain)
                django_locale_code = to_locale(lang_plus_domain)
                # django_language_code=zh-hans django_locale_code=zh_Hans

                dir = f"locale.licenses/{django_locale_code}/LC_MESSAGES"

                po_filename = f"{domain}.po"
                if not os.path.isdir(dir):
                    os.makedirs(dir)
                pofile.save(os.path.join(dir, po_filename))
                print(f"Created {os.path.join(dir, po_filename)}")

                # Compile the messages to a .mo file so we can load them in the next step.
                mo_filename = f"{domain}.mo"
                pofile.save_as_mofile(os.path.join(dir, mo_filename))
                print(f"Created {os.path.join(dir, mo_filename)}")

                # To double-check, make sure we can load the translations in a way that Django would
                # if we were going to use them.

                # def translation(domain, localedir=None, languages=None,
                #                 class_=None, fallback=False, codeset=None):
                gettext.Catalog(
                    domain=domain,
                    languages=[django_language_code],
                    localedir="locale.licenses",
                    codeset="utf-8",
                )

                # DjangoTranslation(
                #     language=django_language_code,
                #     domain=domain,
                #     localedirs=["locale.licenses"],
                # )

    def import_by_40_license_html(self, content, license_code, language_code):
        """
        Returns a dictionary mapping our internal keys to strings.
        """
        messages = {}
        # print(f"Importing {license_code} {version} {language_code}")
        print(f"Importing {license_code} {language_code}")
        raw_html = content
        # Some trivial making consistent - some translators changed 'strong' to 'b'
        # for some unknown reason.
        raw_html = raw_html.replace("<b>", "<strong>").replace("</b>", "</strong>")
        raw_html = raw_html.replace("<B>", "<strong>").replace("</B>", "</strong>")

        # Parse the raw HTML to a BeautifulSoup object.
        soup = BeautifulSoup(raw_html, "lxml")

        # Get the license titles and intro text.

        deed_main_content = soup.find(id="deed-main-content")
        messages["license_medium"] = inner_html(soup.find(id="deed-license").h2)
        messages["license_long"] = inner_html(deed_main_content.h3)
        messages["license_intro"] = inner_html(
            deed_main_content.h3.find_next_sibling("p")
        )

        # Section 1 – Definitions.

        # We're going to work out a list of what definitions we expect in this license,
        # and in what order.
        # Start with the definitions common to all the BY 4.0 licenses
        expected_definitions = [
            "adapted_material",
            "copyright_and_similar_rights",
            "effective_technological_measures",
            "exceptions_and_limitations",
            "licensed_material",
            "licensed_rights",
            "licensor",
            "share",
            "sui_generis_database_rights",
            "you",
        ]

        # now insert the optional ones
        def insert_after(after_this, what_to_insert):
            i = expected_definitions.index(after_this)
            expected_definitions.insert(i + 1, what_to_insert)

        if license_code == "by-sa":
            insert_after("adapted_material", "adapters_license")
            insert_after("adapters_license", "by_sa_compatible_license")
            insert_after("exceptions_and_limitations", "license_elements_sa")
            # See https://github.com/creativecommons/creativecommons.org/issues/1153
            # BY-SA 4.0 for "pt" has an extra definition. Work around for now.
            if language_code == "pt":
                insert_after("you", "you2")
        elif license_code == "by":
            insert_after("adapted_material", "adapters_license")
        elif license_code == "by-nc":
            insert_after("adapted_material", "adapters_license")
            insert_after("licensor", "noncommercial")
        elif license_code == "by-nd":
            pass
        elif license_code == "by-nc-nd":
            insert_after("licensor", "noncommercial")
        elif license_code == "by-nc-sa":
            insert_after("adapted_material", "adapters_license")
            insert_after("exceptions_and_limitations", "license_elements_nc_sa")
            insert_after("adapters_license", "by_nc_sa_compatible_license")
            insert_after("licensor", "noncommercial")

        # definitions are in an "ol" that is the next sibling of the id=s1 element.
        messages["s1_definitions_title"] = inner_html(soup.find(id="s1").strong)
        for i, definition in enumerate(
            soup.find(id="s1").find_next_siblings("ol")[0].find_all("li")
        ):
            thing = name_and_text(definition)
            defn_key = expected_definitions[i]
            messages[
                f"s1_definitions_{defn_key}"
            ] = f"*{thing['name']}* {thing['text']}"

        # Section 2 – Scope.
        messages["s2_scope"] = inner_html(soup.find(id="s2").strong)

        # Section 2a - License Grant
        # translation of "License grant"
        s2a = soup.find(id="s2a")
        if s2a.strong:
            messages["s2a_license_grant_title"] = inner_html(s2a.strong)
        elif s2a.b:
            messages["s2a_license_grant_title"] = inner_html(s2a.b)
        else:
            print(f"How do I handle {s2a}?")
            sys.exit(1)

        # s2a1: rights
        messages["s2a_license_grant_intro"] = str(list(soup.find(id="s2a1"))[0]).strip()

        if "nc" in license_code:
            messages["s2a_license_grant_share_nc"] = str(
                list(soup.find(id="s2a1A"))[0]
            ).strip()
        else:
            messages["s2a_license_grant_share"] = str(
                list(soup.find(id="s2a1A"))[0]
            ).strip()

        if "nc" in license_code and "nd" in license_code:
            messages["s2a_license_grant_adapted_nc_nd"] = str(
                list(soup.find(id="s2a1B"))[0]
            ).strip()
        elif "nc" in license_code:
            messages["s2a_license_grant_adapted_nc"] = str(
                list(soup.find(id="s2a1B"))[0]
            ).strip()
        elif "nd" in license_code:
            messages["s2a_license_grant_adapted_nd"] = str(
                list(soup.find(id="s2a1B"))[0]
            ).strip()
        else:
            messages["s2a_license_grant_adapted"] = str(
                list(soup.find(id="s2a1B"))[0]
            ).strip()

        # s2a2: exceptions
        nt = name_and_text(soup.find(id="s2a2"))
        messages["s2a2_license_grant_exceptions_name"] = nt["name"]
        messages["s2a2_license_grant_exceptions_text"] = nt["text"]

        # s2a3: term
        nt = name_and_text(soup.find(id="s2a3"))
        messages["s2a3_license_grant_term_name"] = nt["name"]
        messages["s2a3_license_grant_term_text"] = nt["text"]

        # s2a4: media
        nt = name_and_text(soup.find(id="s2a4"))
        messages["s2a4_license_grant_media_name"] = nt["name"]
        messages["s2a4_license_grant_media_text"] = nt["text"]

        # s2a5: scope/grant/downstream
        messages["s2a5_license_grant_downstream_title"] = str(
            soup.find(id="s2a5").strong
        )

        expected_downstreams = [
            "offer",
            "no_restrictions",
        ]
        if license_code in ["by-sa", "by-nc-sa"]:
            expected_downstreams.insert(1, "adapted_material")

        # Process top-level "li" elements under the ol
        for i, li in enumerate(
            soup.find(id="s2a5").div.ol.find_all("li", recursive=False)
        ):
            key = expected_downstreams[i]
            thing = name_and_text(li)
            messages[f"s2a5_license_grant_downstream_{key}_name"] = thing["name"]
            messages[f"s2a5_license_grant_downstream_{key}_text"] = thing["text"]

        nt = name_and_text(soup.find(id="s2a6"))
        messages["s2a6_license_grant_no_endorsement_name"] = nt["name"]
        messages["s2a6_license_grant_no_endorsement_text"] = nt["text"]

        # s2b: other rights
        messages["s2b_other_rights_title"] = text_up_to(soup.find(id="s2b"), "ol")
        text_items = soup.find(id="s2b").ol.find_all("li", recursive=False)
        messages["s2b_other_rights_moral"] = str(text_items[0])
        messages["s2b_other_rights_patent"] = str(text_items[1])
        if "nc" in license_code:
            messages["s2b_other_rights_waive_nc"] = str(text_items[2])
        else:
            messages["s2b_other_rights_waive_non_nc"] = str(text_items[2])

        # Section 3: conditions
        messages["s3_conditions_title"] = nested_text(soup.find(id="s3"))
        messages["s3_conditions_intro"] = nested_text(
            soup.find(id="s3").find_next_sibling("p")
        )
        s3a = soup.find(id="s3a")
        messages["s3_conditions_attribution"] = text_up_to(s3a, "ol")

        if "nd" in license_code:
            messages["s3_conditions_if_you_share_nd"] = text_up_to(
                soup.find(id="s3a1"), "ol"
            )
        else:
            messages["s3_conditions_if_you_share_non_nd"] = text_up_to(
                soup.find(id="s3a1"), "ol"
            )

        messages["s3_conditions_retain_the_following"] = text_up_to(
            soup.find(id="s3a1A"), "ol"
        )
        messages["s3_conditions_identification"] = nested_text(soup.find(id="s3a1Ai"))
        messages["s3_conditions_copyright"] = str(soup.find(id="s3a1Aii"))
        messages["s3_conditions_license"] = str(soup.find(id="s3a1Aiii"))
        messages["s3_conditions_disclaimer"] = str(soup.find(id="s3a1Aiv"))
        messages["s3_conditions_link"] = str(soup.find(id="s3a1Av"))
        messages["s3_conditions_modified"] = str(soup.find(id="s3a1B"))
        messages["s3_conditions_licensed"] = str(soup.find(id="s3a1C"))
        messages["s3_conditions_satisfy"] = list(soup.find(id="s3a2"))[0].string
        messages["s3_conditions_remove"] = list(soup.find(id="s3a3"))[0].string

        # share-alike is only in some licenses
        if license_code.endswith("-sa"):
            messages["sharealike_name"] = nested_text(soup.find(id="s3b").strong)
            messages["sharealike_intro"] = nested_text(soup.find(id="s3b").p)

        # Section 4: Sui generis database rights
        messages["s4_sui_generics_database_rights_titles"] = nested_text(
            soup.find(id="s4")
        )
        messages["s4_sui_generics_database_rights_intro"] = (
            soup.find(id="s4").find_next_sibling("p").string
        )
        if "nc" in license_code and "nd" in license_code:
            messages[
                "s4_sui_generics_database_rights_extract_reuse_nc_nd"
            ] = nested_text(soup.find(id="s4a"))
        elif "nc" in license_code:
            messages["s4_sui_generics_database_rights_extract_reuse_nc"] = str(
                soup.find(id="s4a")
            )
        elif "nd" in license_code:
            messages["s4_sui_generics_database_rights_extract_reuse_nd"] = str(
                soup.find(id="s4a")
            )
        else:
            messages[
                "s4_sui_generics_database_rights_extract_reuse_non_nc_non_nd"
            ] = soup.find(id="s4a").get_text()
        s4b = soup.find(id="s4b").get_text()
        if license_code.endswith("-sa"):
            messages["s4_sui_generics_database_rights_adapted_material_sa"] = s4b
        else:
            messages["s4_sui_generics_database_rights_adapted_material_non-sa"] = s4b
        messages["s4_sui_generics_database_rights_comply_s3a"] = soup.find(
            id="s4c"
        ).get_text()
        # The next text comes after the 'ol' after s4, but isn't inside a tag itself!
        parent = soup.find(id="s4").parent
        s4_seen = False
        take_next = False
        for item in parent.children:
            if take_next:
                messages["s4_sui_generics_database_rights_postscript"] = item.string
                break
            elif not s4_seen:
                if isinstance(item, Tag) and item.get("id") == "s4":
                    s4_seen = True
                    continue
            elif not take_next and item.name == "ol":
                # already seen s4, this is the ol, so the next child is our text
                take_next = True

        # Section 5: Disclaimer
        messages["s5_disclaimer_title"] = soup.find(id="s5").string
        messages["s5_a"] = soup.find(id="s5a").string  # bold
        messages["s5_b"] = soup.find(id="s5b").string  # bold
        messages["s5_c"] = soup.find(id="s5c").string  # not bold

        # Section 6: Term and Termination
        messages["s6_termination_title"] = nested_text(soup.find(id="s6"))
        messages["s6_termination_applies"] = nested_text(soup.find(id="s6a"))
        s6b = soup.find(id="s6b")
        if s6b.p:
            # most languages put the introductory text in a paragraph, making it easy
            messages["s6_termination_reinstates_where"] = soup.find(
                id="s6b"
            ).p.get_text()
        else:
            # if they don't, we have to pick out the text from the beginning of s6b's
            # content until the beginning of the "ol" inside it.
            s = ""
            for child in s6b:
                if child.name == "ol":
                    break
                s += str(child)
            messages["s6_termination_reinstates_where"] = s
        messages["s6_termination_reinstates_automatically"] = soup.find(
            id="s6b1"
        ).get_text()
        messages["s6_termination_reinstates_express"] = soup.find(id="s6b2").get_text()

        children_of_s6b = list(soup.find(id="s6b").children)
        messages["s6_termination_reinstates_postscript"] = (
            "".join(str(x) for x in children_of_s6b[4:7])
        ).strip()

        # Section 7: Other terms and conditions
        messages["s7_other_terms_title"] = soup.find(id="s7").string
        messages["s7_a"] = soup.find(id="s7a").string
        messages["s7_b"] = soup.find(id="s7b").string

        # Section 8: Interpretation
        messages["s8_interpretation_title"] = soup.find(id="s8").string
        for key in ["s8a", "s8b", "s8c", "s8d"]:
            messages[key] = inner_html(soup.find(id=key))

        validate_dictionary_is_all_text(messages)

        return messages
