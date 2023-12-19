class Gerrit():
    """ Gerrit code review API connector

    Populates the smartSHARK backend with code review data from Gerrit.
    """

    # people cache
    people_cache: dict = {}

    def __init__(self, config, project, review_system):
        self.config = config
        self.project = project
        self.review_system = review_system

    def run(self):
        """ Executes the complete workflow
        
        """

        # go

    def fetch_review_list(self):
        """ Fetches all reviews for the project
        
        """

        # go

    def _store_review(self, raw_review):
        """ Stores a review in the database
        
        """

        # go