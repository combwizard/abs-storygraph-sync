import unittest

from bs4 import BeautifulSoup

from app import (
    _authors_compatible,
    _item_to_book,
    _normalize_book_title,
    _pick_book_id_from_search,
    _search_authors,
    _search_titles,
    _strip_narrators_from_authors,
    _strip_series_from_title,
    _titles_compatible,
)


def _pane(book_id, title, author, series=None, extra=""):
    series_html = ""
    if series:
        name, num = series
        series_html = (
            f'<p><a href="/series/1">{name}</a> <a href="/series/1">{num}</a></p>'
        )
    return f"""
    <div class="book-pane" data-book-id="{book_id}">
      <div class="book-title-author-and-series">
        {series_html}
        <h3 class="font-bold text-xl">
          <a href="/books/{book_id}">{title}</a>
          <p><a href="/authors/abc">{author}</a></p>
        </h3>
      </div>
      <p>{extra}</p>
    </div>
    """


ROADKILL_SEARCH = f"""
<html><body>
{_pane("florida-id", "Florida Roadkill", "Tim Dorsey", ("Serge Storms", "#1"), "paperback 1999 13 editions")}
{_pane("thurman-id", "Roadkill", "Rob Thurman", ("Cal Leandros", "#5"), "4 editions")}
{_pane("antiques-id", "Antiques Roadkill", "Barbara Allan", ("A Trash n Treasures Mystery", "#1"))}
{_pane("taylor-id", "Roadkill", "Dennis E. Taylor", None, "audio 2022 8 editions ISBN/UID: B0B6QBNK4J")}
{_pane("friedman-id", "Roadkill", "Kinky Friedman", ("Kinky Friedman", "#10"))}
</body></html>
"""


class TitleNormTests(unittest.TestCase):
    def test_strips_narrator_parens_and_articles(self):
        self.assertEqual(
            _normalize_book_title("Lock In (Narrated by Wil Wheaton)"),
            "lock in",
        )
        self.assertEqual(_normalize_book_title("The Silent Patient"), "silent patient")
        self.assertEqual(_normalize_book_title("CATCH-22"), "catch 22")

    def test_roadkill_is_not_florida_roadkill(self):
        self.assertTrue(_titles_compatible("Roadkill", "Roadkill"))
        self.assertFalse(_titles_compatible("Roadkill", "Florida Roadkill"))
        self.assertFalse(_titles_compatible("Roadkill", "Antiques Roadkill"))
        self.assertTrue(_titles_compatible("Lock In", "Lock In: A Novel"))
        self.assertTrue(_titles_compatible("Project Hail Mary: A Novel", "Project Hail Mary"))
        self.assertTrue(_titles_compatible("Rebellion Frontiers Saga Part 2", "Rebellion"))
        self.assertTrue(_titles_compatible("A Rock and a Hard Place Frontiers Saga, Part 2", "A Rock and a Hard Place"))

    def test_authors(self):
        self.assertTrue(_authors_compatible("Dennis E. Taylor", "Dennis E. Taylor"))
        self.assertFalse(_authors_compatible("Dennis E. Taylor", "Tim Dorsey"))
        self.assertTrue(_authors_compatible("Scott Moon, J.N. Chaney", "Scott Moon, J.N. Chaney"))
        self.assertTrue(_authors_compatible("Scott Moon, J.N. Chaney", "Scott Moon"))
        self.assertTrue(_authors_compatible("B.V. Larson", "B.V. Larson, David VanDyke"))


class SeriesStripTests(unittest.TestCase):
    def test_strips_frontiers_saga_part_from_title(self):
        self.assertEqual(
            _strip_series_from_title(
                "Rebellion Frontiers Saga Part 2",
                "Frontiers Saga, Part 2: Rogue Castes #4",
            ),
            "Rebellion",
        )
        self.assertEqual(
            _strip_series_from_title(
                "A Rock and a Hard Place Frontiers Saga, Part 2",
                "Frontiers Saga, Part 2: Rogue Castes #11",
            ),
            "A Rock and a Hard Place",
        )
        self.assertEqual(
            _strip_series_from_title("Rebellion Frontiers Saga Part 2"),
            "Rebellion",
        )

    def test_leaves_clean_titles_alone(self):
        self.assertEqual(
            _strip_series_from_title("The Rings of Haven", "The Frontiers Saga #2"),
            "The Rings of Haven",
        )
        self.assertEqual(_strip_series_from_title("Roadkill"), "Roadkill")
        self.assertEqual(
            _strip_series_from_title("Harry Potter and the Deathly Hallows Part 2"),
            "Harry Potter and the Deathly Hallows Part 2",
        )

    def test_search_titles_try_core_first(self):
        queries = _search_titles(
            "Rebellion Frontiers Saga Part 2",
            "Frontiers Saga, Part 2: Rogue Castes #4",
            "Rogue Castes, Book 4",
        )
        self.assertEqual(queries[0], "Rebellion")
        self.assertIn("Rebellion Frontiers Saga Part 2", queries)
        self.assertNotIn("Rogue Castes", queries)

    def test_series_only_title_falls_back_to_subtitle(self):
        queries = _search_titles(
            "Frontiers Saga Part 3",
            "Frontiers Saga, Part 3: Fringe Worlds #3",
            "Liberty and Truth, Peace and Prosperity Fringe Worlds Series, Book 3",
        )
        self.assertEqual(queries[0], "Liberty and Truth, Peace and Prosperity")


class SearchPickTests(unittest.TestCase):
    def setUp(self):
        self.soup = BeautifulSoup(ROADKILL_SEARCH, "html.parser")

    def test_florida_roadkill_does_not_win_for_taylor(self):
        chosen = _pick_book_id_from_search(
            self.soup, "Roadkill", "Dennis E. Taylor"
        )
        self.assertEqual(chosen, "taylor-id")

    def test_asin_picks_taylor_even_if_listed_last(self):
        chosen = _pick_book_id_from_search(
            self.soup, "Roadkill", "Dennis E. Taylor", identifiers=["B0B6QBNK4J"]
        )
        self.assertEqual(chosen, "taylor-id")

    def test_substring_title_without_author_is_not_florida(self):
        chosen = _pick_book_id_from_search(self.soup, "Roadkill", "")
        # Several exact "Roadkill" titles by different authors → refuse
        self.assertIsNone(chosen)

    def test_title_match_wrong_author_refused(self):
        chosen = _pick_book_id_from_search(
            self.soup, "Roadkill", "Tim Dorsey"
        )
        # "Roadkill" panes exist, but Tim Dorsey's book is Florida Roadkill
        self.assertIsNone(chosen)

    def test_author_required_even_for_unique_soft_title(self):
        soup = BeautifulSoup(
            f"""
            <html><body>
            {_pane("x-id", "Lock In: A Novel", "John Scalzi")}
            </body></html>
            """,
            "html.parser",
        )
        self.assertEqual(
            _pick_book_id_from_search(soup, "Lock In", "John Scalzi"),
            "x-id",
        )
        self.assertIsNone(_pick_book_id_from_search(soup, "Lock In", "Someone Else"))

    def test_unknown_title_returns_none(self):
        chosen = _pick_book_id_from_search(self.soup, "Heaven's River", "Dennis E. Taylor")
        self.assertIsNone(chosen)

    def test_series_glued_title_matches_short_storygraph_title(self):
        soup = BeautifulSoup(
            f"""
            <html><body>
            {_pane("rebellion-id", "Rebellion", "Ryk Brown", ("The Frontiers Saga, Part 2: Rogue Castes", "#4"))}
            {_pane("ep-id", "Ep.#4 - Rebellion", "Ryk Brown")}
            </body></html>
            """,
            "html.parser",
        )
        chosen = _pick_book_id_from_search(
            soup,
            "Rebellion Frontiers Saga Part 2",
            "Ryk Brown",
            alt_titles=["Rebellion", "Rebellion Frontiers Saga Part 2"],
        )
        self.assertEqual(chosen, "rebellion-id")


class ItemToBookTests(unittest.TestCase):
    def test_skips_podcasts_and_keeps_asin(self):
        self.assertIsNone(_item_to_book(
            {"mediaType": "podcast", "media": {"metadata": {"title": "Rebel FM"}}},
            {"progress": 0.2},
        ))
        book = _item_to_book(
            {
                "mediaType": "book",
                "media": {
                    "metadata": {
                        "title": "Roadkill",
                        "authorName": "Dennis E. Taylor",
                        "asin": "B0B6QBNK4J",
                        "seriesName": "Bobiverse #2",
                    },
                    "duration": 3600,
                },
            },
            {"progress": 0.146, "currentTime": 500},
        )
        self.assertEqual(book["title"], "Roadkill")
        self.assertEqual(book["asin"], "B0B6QBNK4J")
        self.assertEqual(book["series_name"], "Bobiverse #2")
        self.assertAlmostEqual(book["progress_percent"], 14.6)
        self.assertFalse(book["prefer_audio"])

    def test_strips_narrator_from_authorname(self):
        book = _item_to_book(
            {
                "mediaType": "book",
                "media": {
                    "metadata": {
                        "title": "Project Hail Mary",
                        "authorName": "Andy Weir, Ray Porter",
                        "narratorName": "Ray Porter",
                        "asin": "B08G9PRS1K",
                    },
                    "duration": 3600,
                },
            },
            {"progress": 0.5},
        )
        self.assertEqual(book["author"], "Andy Weir")
        self.assertEqual(book["narrators"], ["Ray Porter"])
        self.assertTrue(book["prefer_audio"])


class AuthorSearchTests(unittest.TestCase):
    def test_drops_narrator_and_keeps_coauthors(self):
        self.assertEqual(
            _strip_narrators_from_authors(
                ["Andy Weir", "Ray Porter"], ["Ray Porter"]
            ),
            ["Andy Weir"],
        )
        self.assertEqual(
            _strip_narrators_from_authors(
                ["Scott Moon", "J.N. Chaney"], ["R.C. Bray"]
            ),
            ["Scott Moon", "J.N. Chaney"],
        )

    def test_search_authors_try_full_then_first_then_none(self):
        self.assertEqual(
            _search_authors("Andy Weir, Ray Porter"),
            ["Andy Weir, Ray Porter", "Andy Weir", ""],
        )
        self.assertEqual(_search_authors("Andy Weir"), ["Andy Weir", ""])



if __name__ == "__main__":
    unittest.main()
