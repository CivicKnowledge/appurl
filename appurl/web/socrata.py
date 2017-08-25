# Copyright (c) 2017 Civic Knowledge. This file is licensed under the terms of the
# MIT, included in this distribution as LICENSE

""" """
from appurl.util import parse_url_to_dict, unparse_url_dict, file_ext
from os.path import basename, join
from appurl.url import Url

class SocrataUrl(Url):
    def __init__(self, url, **kwargs):
        kwargs['resource_format'] = 'csv'
        kwargs['encoding'] = 'utf8'
        kwargs['proto'] = 'socrata'

        super(SocrataUrl, self).__init__(url, **kwargs)

    match_priority = 10

    @classmethod
    def match(cls, url, **kwargs):
        return url.proto == 'socrata'

    def _process_resource_url(self):
        self.resource_url = unparse_url_dict(self.parts.__dict__,
                                             scheme_extension=False,
                                             fragment=False,
                                             path=join(self.parts.path, 'rows.csv'))

        self.resource_file = basename(self.url) + '.csv'

        if self.resource_format is None:
            self.resource_format = file_ext(self.resource_file)

        self.target_file = self.resource_file  # _process_target() file will use this self.target_file
