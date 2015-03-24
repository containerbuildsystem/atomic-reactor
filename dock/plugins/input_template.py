"""
Reads the template from template paths and replace stuff in it
Usage:
dock --input template --input-arg template_path=/tmp/template.json --input-arg key1=value1 --input-arg key2=value2 

"""
import json

from dock.plugin import InputPlugin

def replace_dict(adict, rkey, rvalue):
    for key in adict.keys():
        if adict[key] == rkey:
            adict[key] = rvalue
        elif type(adict[key]) is dict:
            replace_dict(adict[key], rkey, rvalue)
        elif type(adict[key]) is list:
            for i in range(len(adict[key])):
                replace_dict(adict[key][i], rkey, rvalue)

class TemplateInputPlugin(InputPlugin):
    key = "template"

    def __init__(self, template_path=None, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(TemplateInputPlugin, self).__init__()
        self.template_path = template_path
        self.kwargs = kwargs

    def run(self):
        """
        open json template and replace stuff in it
        """
        try:
            with open(self.template_path, 'r') as build_cfg_fd:
                build_cfg_json = json.load(build_cfg_fd)
        except ValueError:
            self.log.error("couldn't decode json from file '%s'", self.template_path)
            return None
        except IOError:
            self.log.error("couldn't read json from file '%s'", self.template_path)
            return None
        else:
            for key, value in self.kwargs.iteritems():
                replace_dict(build_cfg_json, key, value)
            self.log.debug(build_cfg_json)
            return build_cfg_json
