# from ruamel import yaml
# from ruamel.yaml import YAML
# # ruamel.yaml.__version__
# # Out[15]: '0.18.6'
#
import sys
from os.path import exists, join
import os
from datetime import datetime
from os.path import dirname
from pprint import pprint

from ruamel.yaml import YAML

# from taLibs import ParamsBase_shared_vars

yaml = YAML()
# yaml.indent(offset=4)
yaml.compact(seq_seq=False, seq_map=False)
# yaml.default_flow_style = False  # Dùng cho YAML đẹp, không theo dạng inline
yaml.indent(mapping=4, sequence=4, offset=4)  # Cấu hình lùi dòng (indentation)
yaml.width = 4096
yml_file_contents = {}


def delkeyVal(mDict, lstmkey):
    for mkey in lstmkey:
        if mkey in mDict:
            del mDict[mkey]
    return mDict


class tactParametters:

    def __init__(self, logdir="", ModuleName="TACT"):
        self.ModuleName = ModuleName
        self.logdir = logdir
        self.Version = "3.1"
        config_filename = 'Stream_cameras_configs.yml'
        # self.fn=ParamsBase_shared_vars.config_filename
        self.fn = config_filename
        # self.fn = os.path.join(os.path.dirname(__file__), self.fn)
        # self.debug_config_path = "AI_Data/debug.yml"

    def to_yaml(self, file_path=''):
        # return
        global yml_file_contents
        # self.fn = self.fn or file_path
        # print("fn:", self.fn, file_path)
        with open(self.fn, "w", encoding="utf-8") as file:
            # mdict = {self.ModuleName: self.__dict__}
            mDict = self.__dict__.copy()
            mDict = delkeyVal(mDict, ["ModuleName", "logdir", "fn", "Version"])
            yml_file_contents[self.ModuleName] = mDict
            yaml.dump(yml_file_contents, file)

    def from_yaml(self, file_path=''):
        global yml_file_contents
        # self.fn = self.fn or file_path
        
        with open(self.fn, "r", encoding="utf-8") as file:
            yml_file_contents = yaml.load(file)
            if yml_file_contents:
                if self.ModuleName in yml_file_contents:
                    data = yml_file_contents[self.ModuleName]
                    self.__dict__.update(data)

    def save_to_yaml_v2(self, file_path="", ModuleName="", flogDict=False):
        self.ModuleName = ModuleName or self.ModuleName
        # self.fn = self.fn or file_path
        if exists(self.fn):
            self.from_yaml(self.fn)
        self.to_yaml(self.fn)
        if flogDict:
            self.ta_print_log(str(self.__dict__))

    @staticmethod
    def fnFIS(mDir, exts=(".jpg", ".jpeg", ".png")):  # mConf.local_log_data_dir_input
        inputTypes_Files = []
        for D, _, F in os.walk(mDir):
            for fn in F:
                if fn.endswith(exts):
                    inputTypes_Files.append(join(D, fn).replace("\\", "/"))
        return inputTypes_Files

    def ta_print_log(self, *args):
        logdir = self.logdir
        margs = []
        for x in args:
            try:
                margs.append(str(x))
            except:
                pass
        s = " ".join(margs)
        currTime = datetime.now()
        sDT = currTime.strftime("%m/%d, %H:%M:%S")
        if logdir:
            fn = f"{logdir}/logs/{currTime.year}/{currTime.month}/{currTime.day}/logs.txt"
        else:
            fn = f"logs/{currTime.year}/{currTime.month}/{currTime.day}/logs.txt"
        os.makedirs(dirname(fn), exist_ok=True)
        with open(fn, "a", encoding="utf-8") as ff:
            ff.write(f"{s}\n")
        print(sDT, s)


if __name__ == "__main__":
    RUN = "Test Normal"
    if RUN == "Test Base":
        mcls = tactParametters()
        mcls.save_to_yaml_v2("tmp.yml")
    if RUN == "Short":
        # from taFuncs.ParamsBase import tactParametters
        class Config(tactParametters):
            def __init__(self):
                super().__init__()

                self.save_to_yaml_v2("system_config.yml", ModuleName="SystemParams")

    if RUN == "Test Normal":

        TACT = 1  #### <<<<<=================== Sửa dòng này =0 để chạy không cần lưu tham số nhá, muốn thêm tham số, chỉ việc thêm vào class bên dưới
        if TACT:
            # from aiLibs.paramettersbase import taParametters, ta_print_log
            pass
        else:

            class taParametters:
                def __init__(self):
                    pass

            def ta_print_log(*args):
                print(*args)

        # ----------------------------------------------------------------------------------------------------------
        class Parameters(tactParametters):
            def __init__(self, ModuleName="TACT"):
                super().__init__()
                self.thamso1 = "thamso1"
                self.thamso2 = " xâu tiếng việt"
                self.api_url = "https://200.168.90.38:6699/avi/collect_data"
                self.testpath = "D:/test_debug_fii"
                self.test_real = 0.8
                self.test_int = 12
                self.test_dict = {
                    1: 2,
                    3: 4.5,
                    "6": "bảy nhá",
                    -1: "Tám",
                    9: [10, 11.2, "22", (33, 44, "55")],
                    10: {101: 12, 102: "mười ba"},
                }
                self.test_list = [1, 2, 3, 4, 5, 6, 7, 8, 9]

                fn = "configs_test.yml"
                self.save_to_yaml_v2(fn, ModuleName=ModuleName)

        mParams = Parameters("test")

        class Parameters(tactParametters):
            def __init__(self, ModuleName="TACT"):
                super().__init__()
                self.thamso1 = "thamso1"
                self.thamso2 = " xâu tiếng việt"
                self.api_url = "https://200.168.90.38:6699/avi/collect_data"
                self.testpath = "D:/test_debug_fii"
                self.test_real = 0.8
                self.test_int = 12
                self.test_dict = {
                    1: 2,
                    3: 4.5,
                    "6": "bảy nhá",
                    -1: "Tám",
                    9: [10, 11.2, "22", (33, 44, "55")],
                    10: {101: 12, 102: "mười ba"},
                }
                self.test_list = [1, 2, 3, 4, 5, 6, 7, 8, 9]

                fn = "configs_test.yml"
                if TACT:
                    self.save_to_yaml_v2(fn, ModuleName=ModuleName)

        mParams = Parameters(ModuleName="Test module")
        mParams1 = Parameters(ModuleName="Test module Mot")
        pprint(mParams.__dict__)
        pprint(mParams1.__dict__)
        print(mParams.api_url)
        print(mParams1.api_url)

        pass

    #
    #
    # inp = """\
    # # example
    # name:
    #   # details
    #   family: Smith   # very common
    #   given: Alice    # one of the siblings
    # """
    # mdict = {
    #     1: 2,
    #     'b': 4,
    #     5: [1, 2, 3, 4, 'b', 5.1],
    #     6: {1: 2, 'b': 5.2}
    # }
    # yaml = YAML()
    # yaml.indent(mapping=4, sequence=6, offset=3)
    # # code = yaml.load(inp)
    # # code['name']['given'] = 'Bob'
    # with open("tmp.yml", "w") as outfile:
    #     yaml.dump(mdict, outfile)
