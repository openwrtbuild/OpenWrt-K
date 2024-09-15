# SPDX-FileCopyrightText: Copyright (c) 2024 沉默の金 <cmzj@cmzj.org>
# SPDX-License-Identifier: MIT
import gzip
import json
import os
import re
import shutil
import tarfile
import tempfile
from multiprocessing.pool import Pool
from typing import TYPE_CHECKING, Any

import pygit2
from actions_toolkit import core
from actions_toolkit.github import Context

from utils.logger import logger
from utils.network import dl2, get_gh_repo_last_releases, request_get, wait_dl_tasks
from utils.openwrt import OpenWrt
from utils.paths import paths
from utils.upload import uploader
from utils.utils import parse_config

if TYPE_CHECKING:
    from pySmartDL import SmartDL



def parse_configs() -> dict[str, dict[str, Any]]:
    """解析配置文件"""
    configs: dict[str, dict] = {}
    for name, path in paths.configs.items():
        logger.info("解析配置: %s", name)
        configs[name] = {"path": path}
        k_config_path = os.path.join(path, "OpenWrt-K")
        if not os.path.isdir(k_config_path):
            core.set_failed(f"未找到配置{name}的openwrt文件夹: {k_config_path}")

        configs[name]["compile"] = parse_config(os.path.join(k_config_path, "compile.config"),
                                                ("openwrt_tag/branch", "kmod_compile_exclude_list", "use_cache"))

        configs[name]["openwrtext"] = parse_config(os.path.join(k_config_path, "openwrtext.config"), ("ipaddr", "timezone", "zonename", "golang_version"))
        extpackages_config = os.path.join(k_config_path, "extpackages.config")
        configs[name]["extpackages"] = {}
        if os.path.isfile(extpackages_config):
            extpackages = {}
            with open(extpackages_config, encoding="utf-8") as f:
                for line in f:
                    matched = re.match(r"^EXT_PACKAGES_(?P<key>NAME|PATH|REPOSITORIE|BRANCH)\[(?P<id>\d+)\]=\"(?P<value>.*?)\"$", line.strip())
                    if matched:
                        if matched.group("id") not in extpackages:
                            extpackages[matched.group("id")] = {}
                        extpackages[matched.group("id")][matched.group("key")] = matched.group("value")
            for i, pkg in extpackages.items():
                pkg: dict
                keys = ("NAME", "PATH", "REPOSITORIE", "BRANCH")
                for key in keys:
                    if key not in pkg:
                        core.set_failed(f"配置{name}的extpackages{i}缺少{key}")
                pkg_name = pkg["NAME"]
                if name not in configs[name]["extpackages"]:
                    configs[name]["extpackages"][pkg["NAME"]] = {k:v for k, v in pkg.items() if k != "NAME"}
                else:
                    core.set_failed(f"配置{name}的extpackages中存在重复的包名: {pkg_name}")

        configs[name]["openwrt"] = ""
        for file in os.listdir(path):
            if os.path.isfile(os.path.join(path, file)) and file.endswith(".config"):
                with open(os.path.join(path, file), encoding="utf-8") as f:
                    configs[name]["openwrt"] += f.read() + "\n"
        if configs[name]["compile"]["use_cache"] is True:
            configs[name]["openwrt"] += "CONFIG_DEVEL=y\nCONFIG_CCACHE=y"
    return configs

def get_matrix(configs: dict[str, dict]) -> str:
    matrix = {"include": []}
    for name, config in configs.items():
        matrix["include"].append({"name": name, "config": json.dumps(config)})
    return json.dumps(matrix)


def prepare(configs: dict[str, dict[str, Any]]) -> None:
    # clone拓展软件源码
    logger.info("开始克隆拓展软件源码...")
    to_clone: set[tuple[str, str]] = {("https://github.com/immortalwrt/packages", ""),
                                       ("https://github.com/chenmozhijin/turboacc", "package"),
                                       ("https://github.com/pymumu/openwrt-smartdns", "master"),
                                       ("https://github.com/pymumu/luci-app-smartdns", "master"),
                                       *[(pkg["REPOSITORIE"], pkg["BRANCH"]) for config in configs.values() for pkg in config["extpackages"].values()],
                                       *[("https://github.com/sbwml/packages_lang_golang",
                                          config["openwrtext"]["golang_version"]) for config in configs.values()]}
    cloned_repos: dict[tuple[str, str], str] = {}
    for repo, branch in to_clone:
        path = os.path.join(paths.workdir, "repos", repo.split("/")[-2], repo.split("/")[-1],branch if branch else "@default@")
        logger.info("开始克隆仓库 %s", repo if not branch else f"{repo} (分支: {branch})")
        pygit2.clone_repository(repo, path, checkout_branch=branch if branch else None, depth=1)
        cloned_repos[(repo, branch)] = path


    logger.info("开始处理拓展软件源码...")
    ext_pkg_paths = {os.path.join(cloned_repos[(pkg["REPOSITORIE"], pkg["BRANCH"])], pkg["PATH"])
                     for config in configs.values() for pkg in config["extpackages"].values()}
    for path in ext_pkg_paths:
        logger.info("处理拓展包 %s", path)
        for root, dirs, files in os.walk(path):

            # 修复Makefile中luci.mk的路径
            for file in files:
                if file == "Makefile":
                    with open(os.path.join(root, file), encoding="utf-8") as f:
                        content = f.read()
                    content = content.replace(r"../../luci.mk", r"$(TOPDIR)/feeds/luci/luci.mk")
                    with open(os.path.join(root, file), "w", encoding="utf-8") as f:
                        f.write(content)
                    logger.info("修复%s中luci.mk的路径", os.path.join(root, file))

            # 创建符号链接以修复中文支持
            for _dir in dirs:
                if _dir == "po":
                    po_path = os.path.join(root, _dir)
                    zh_hans = os.path.join(po_path , "zh_Hans")
                    zh_cn = os.path.join(po_path , "zh-cn")
                    if not os.path.isdir(zh_cn):
                        if os.path.isdir(zh_hans) or os.path.islink(zh_hans):
                            logger.debug("已存在符号链接或目录 %s，跳过", zh_hans)
                            continue
                        if os.path.isfile(zh_hans):
                            logger.debug("已存在文件 %s，删除", zh_hans)
                            os.remove(zh_hans)
                        os.symlink("zh_Hans", zh_cn, target_is_directory=True)
                        logger.info("创建符号链接 %s -> %s", zh_cn, zh_hans)
                    elif not os.path.isdir(zh_hans) or not os.path.islink(zh_hans):
                        logger.info("%s 中不存在汉化文件，这可能是该luci插件原生为中文或不支持中文", po_path)


    logger.info("开始克隆openwrt源码...")
    openwrt_paths = os.path.join(paths.workdir, "openwrts")
    cfg_names = list(configs.keys())
    pygit2.clone_repository("https://github.com/openwrt/openwrt", os.path.join(openwrt_paths, cfg_names[0]))
    logger.info("开始更新feeds...")
    openwrt = OpenWrt(os.path.join(openwrt_paths, cfg_names[0]))
    openwrt.feed_update()

    logger.info("开始更新netdata、smartdns...")
    # 更新netdata
    shutil.rmtree(os.path.join(openwrt.path, "feeds", "admin", "netdata"), ignore_errors=True)
    shutil.copytree(os.path.join(cloned_repos[("https://github.com/immortalwrt/packages", "")], "admin", "netdata"),
                        os.path.join(openwrt.path, "feeds", "admin", "netdata"), symlinks=True)
    # 更新smartdns
    shutil.rmtree(os.path.join(openwrt.path, "feeds", "luci", "applications", "luci-app-smartdns"), ignore_errors=True)
    shutil.rmtree(os.path.join(openwrt.path, "feeds", "packages", "net", "smartdns"), ignore_errors=True)
    shutil.copytree(cloned_repos[("https://github.com/pymumu/luci-app-smartdns", "master")],
                    os.path.join(openwrt.path, "feeds", "luci", "applications", "luci-app-smartdns"), symlinks=True)
    shutil.copytree(cloned_repos[("https://github.com/pymumu/openwrt-smartdns", "master")],
                    os.path.join(openwrt.path, "feeds", "packages", "net", "smartdns"), symlinks=True)

    # 复制源码
    if len(cfg_names) > 1:
        for name in cfg_names[1:]:
            shutil.copytree(os.path.join(openwrt_paths, cfg_names[0]), os.path.join(openwrt_paths, name), symlinks=True)
    openwrts = {name: OpenWrt(os.path.join(openwrt_paths, name), configs[name]["compile"]["openwrt_tag/branch"]) for name in cfg_names}

    # 下载AdGuardHome规则与配置
    logger.info("下载AdGuardHome规则与配置...")
    global_files_path = os.path.join(paths.root, "files")
    adg_filters_path = os.path.join(global_files_path, "usr", "bin", "AdGuardHome", "data", "filters")
    os.makedirs(adg_filters_path, exist_ok=True)
    filters = {"1628750870.txt": "https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt",
               "1628750871.txt": "https://anti-ad.net/easylist.txt",
               "1677875715.txt": "https://easylist-downloads.adblockplus.org/easylist.txt",
               "1677875716.txt": "https://easylist-downloads.adblockplus.org/easylistchina.txt",
               "1677875717.txt": "https://raw.githubusercontent.com/cjx82630/cjxlist/master/cjx-annoyance.txt",
               "1677875718.txt": "https://raw.githubusercontent.com/zsakvo/AdGuard-Custom-Rule/master/rule/zhihu-strict.txt",
               "1677875720.txt": "https://gist.githubusercontent.com/Ewpratten/a25ae63a7200c02c850fede2f32453cf/raw/b9318009399b99e822515d388b8458557d828c37/hosts-yt-ads",
               "1677875724.txt": "https://raw.githubusercontent.com/banbendalao/ADgk/master/ADgk.txt",
               "1677875725.txt": "https://www.i-dont-care-about-cookies.eu/abp/",
               "1677875726.txt": "https://raw.githubusercontent.com/jdlingyu/ad-wars/master/hosts",
               "1677875727.txt": "https://raw.githubusercontent.com/Goooler/1024_hosts/master/hosts",
               "1677875728.txt": "https://winhelp2002.mvps.org/hosts.txt",
               "1677875733.txt": "https://raw.githubusercontent.com/hl2guide/Filterlist-for-AdGuard/master/filter_whitelist.txt",
               "1677875734.txt": "https://raw.githubusercontent.com/hg1978/AdGuard-Home-Whitelist/master/whitelist.txt",
               "1677875735.txt": "https://raw.githubusercontent.com/mmotti/adguard-home-filters/master/whitelist.txt",
               "1677875737.txt": "https://raw.githubusercontent.com/liwenjie119/adg-rules/master/white.txt",
               "1677875739.txt": "https://raw.githubusercontent.com/JamesDamp/AdGuard-Home---Personal-Whitelist/master/AdGuardHome-Whitelist.txt",
               #"1677875740.txt": "https://raw.githubusercontent.com/scarletbane/AdGuard-Home-Whitelist/main/whitelist.txt"
    }
    dl_tasks: list[SmartDL] = []
    for name, url in filters.items():
        dl_tasks.append(dl2(url, os.path.join(adg_filters_path, name)))

    dl_tasks.append(dl2("https://raw.githubusercontent.com/chenmozhijin/AdGuardHome-Rules/main/AdGuardHome-dnslist(by%20cmzj).yaml",
                     os.path.join(global_files_path, "etc", "AdGuardHome-dnslist(by cmzj).yaml")))

    wait_dl_tasks(dl_tasks)

        # 获取用户信息
    compiler = Context().repo.owner
    if user_info := request_get(f"https://api.github.com/users/{compiler}"):
        compiler = json.loads(user_info).get("name", compiler)
    logger.info("编译者：%s", compiler)

    tasks = []
    for cfg_name, openwrt in openwrts.items():
        config = configs[cfg_name]
        tasks.append((config, cfg_name, openwrt, cloned_repos, global_files_path, compiler))
    with Pool(len(cfg_names)) as p:
        for cfg_name, config, tar_path in p.starmap(prepare_cfg, tasks):
            configs[cfg_name] = config
            uploader.add(f"{cfg_name}-openwrt-source", tar_path,retention_days=1,compression_level=0)
            logger.info("%s处理完成", cfg_name)


def prepare_cfg(config: dict[str, Any],
                cfg_name: str, openwrt: OpenWrt,
                cloned_repos: dict[tuple[str, str], str],
                global_files_path: str,
                compiler: str):
    logger.info("%s处理软件包...", cfg_name)
    for pkg_name, pkg in config["extpackages"].items():
        path = os.path.join(openwrt.path, "package", "cmzj_packages", pkg_name)
        logger.debug("复制拓展软件包 %s 到 %s", pkg_name, path)
        shutil.copytree(os.path.join(cloned_repos[(pkg["REPOSITORIE"], pkg["BRANCH"])], pkg["PATH"]), path, symlinks=True)
        if os.path.isdir(os.path.join(path, ".git")):
            shutil.rmtree(os.path.join(path, ".git"))

    # 替换golang版本
    golang_path = os.path.join(openwrt.path, "feeds", "packages", "lang", "golang")
    shutil.rmtree(golang_path)
    shutil.copytree(cloned_repos[("https://github.com/sbwml/packages_lang_golang", config["openwrtext"]["golang_version"])], golang_path)
    openwrt.feed_install()
    # 修复问题
    openwrt.fix_problems()
    # 应用配置
    openwrt.apply_config(config["openwrt"])
    openwrt.make_defconfig()
    config["openwrt"] = openwrt.get_diff_config()

    # 添加turboacc补丁
    turboacc_dir = os.path.join(cloned_repos[("https://github.com/chenmozhijin/turboacc", "package")])
    versions = parse_config(os.path.join(turboacc_dir, "version"), ("FIREWALL4_VERSION", "NFTABLES_VERSION", "LIBNFTNL_VERSION"))
    kernel_version = openwrt.get_kernel_version()
    enable_sfe = (openwrt.get_package_config("kmod-shortcut-fe") in ("y", "m") or
               openwrt.get_package_config("kmod-shortcut-fe-drv") in ("y", "m") or
               openwrt.get_package_config("kmod-shortcut-fe-cm") in ("y", "m") or
               openwrt.get_package_config("kmod-fast-classifier") in ("y", "m"))
    enable_fullcone = openwrt.get_package_config("kmod-nft-fullcone") in ("y", "m")
    if enable_fullcone or enable_sfe:
        logger.info("添加952补丁")
        patch925 = f"952{"-add" if kernel_version != "5.10" else ""}-net-conntrack-events-support-multiple-registrant.patch"
        shutil.copy2(os.path.join(turboacc_dir, f"hack-{kernel_version}", patch925),
                     os.path.join(openwrt.path, "target", "linux", "generic", f"hack-{kernel_version}", patch925))
        logger.info("附加内核配置CONFIG_NF_CONNTRACK_CHAIN_EVENTS")
        with open(os.path.join(openwrt.path, "target", "linux", "generic", f"config-{kernel_version}"), "a") as f:
            f.write("\n# CONFIG_NF_CONNTRACK_CHAIN_EVENTS is not set")
    if enable_sfe:
        logger.info("添加953补丁")
        patch953 = "953-net-patch-linux-kernel-to-support-shortcut-fe.patch"
        shutil.copy2(os.path.join(turboacc_dir, f"hack-{kernel_version}", patch953),
                     os.path.join(openwrt.path, "target", "linux", "generic", f"hack-{kernel_version}", patch953))
        logger.info("添加613补丁")
        patch613 = "613-netfilter_optional_tcp_window_check.patch"
        shutil.copy2(os.path.join(turboacc_dir, f"pending-{kernel_version}", patch613),
                     os.path.join(openwrt.path, "target", "linux", "generic", f"pending-{kernel_version}", patch613))
        logger.info("附加内核配置CONFIG_SHORTCUT_FE")
        with open(os.path.join(openwrt.path, "target", "linux", "generic", f"config-{kernel_version}"), "a") as f:
            f.write("\nCONFIG_SHORTCUT_FE=y")
    if enable_fullcone:
        logger.info("添加libnftnl、firewall4、nftables补丁")
        shutil.rmtree(os.path.join(openwrt.path, "package", "libs", "libnftnl"))
        shutil.copytree(os.path.join(turboacc_dir, f"libnftnl-{versions['LIBNFTNL_VERSION']}"), os.path.join(openwrt.path, "package", "libs", "libnftnl"))
        shutil.rmtree(os.path.join(openwrt.path, "package", "network", "config", "firewall4"))
        shutil.copytree(os.path.join(turboacc_dir, f"firewall4-{versions['FIREWALL4_VERSION']}"),
                        os.path.join(openwrt.path, "package", "network", "config", "firewall4"))
        shutil.rmtree(os.path.join(openwrt.path, "package", "network", "utils", "nftables"))
        shutil.copytree(os.path.join(turboacc_dir, f"nftables-{versions['NFTABLES_VERSION']}"),
                        os.path.join(openwrt.path, "package", "network", "utils", "nftables"))

    logger.info("%s准备自定义文件...", cfg_name)
    files_path = os.path.join(openwrt.path, "files")
    shutil.copytree(global_files_path, files_path)
    arch, version = openwrt.get_arch()
    match arch:
        case "i386":
            adg_arch, clash_arch = "386", "linux-386"
        case "i686":
            adg_arch, clash_arch = "386", None
        case "x86_64":
            adg_arch, clash_arch = "amd64", "linux-amd64"
        case "mipsel":
            adg_arch, clash_arch = "mipsel", "linux-mipsle-softfloat"
        case "mips64el":
            adg_arch, clash_arch = "mips64el", None
        case "mips":
            adg_arch, clash_arch = "mips", "linux-mips-softfloat"
        case "mips64":
            adg_arch, clash_arch = "mips64", "linux-mips64"
        case "arm":
            if version:
                adg_arch, clash_arch = f"arm{version}", f"linux-arm{version}"
            else:
                adg_arch, clash_arch = "armv5", "linux-armv5"
        case "aarch64":
            adg_arch, clash_arch = "arm64", "linux-arm64"
        case "powerpc":
            adg_arch, clash_arch = "powerpc", None
        case "powerpc64":
            adg_arch, clash_arch = "ppc64", None
        case _:
            adg_arch, clash_arch = None, None

    tmpdir = tempfile.TemporaryDirectory()
    dl_tasks: list[SmartDL] = []
    if adg_arch and openwrt.get_package_config("luci-app-adguardhome") == "y":
        logger.info("%s下载架构为%s的AdGuardHome核心", cfg_name, adg_arch)
        releases = get_gh_repo_last_releases("AdguardTeam/AdGuardHome")
        if releases:
            for asset in releases["assets"]:
                if asset["name"] == f"AdGuardHome_linux_{adg_arch}.tar.gz":
                    dl_tasks.append(dl2(asset["browser_download_url"], os.path.join(tmpdir.name, "AdGuardHome.tar.gz")))
                    break
            else:
                logger.error("未找到可用的AdGuardHome二进制文件")

    if clash_arch and openwrt.get_package_config("luci-app-openclash") == "y":
        logger.info("%s下载架构为%s的OpenClash核心", cfg_name, clash_arch)
        versions = request_get("https://raw.githubusercontent.com/vernesong/OpenClash/core/master/core_version")
        tun_v = versions.splitlines()[1] if versions else None
        if tun_v:
            dl_tasks.append(dl2(f"https://raw.githubusercontent.com/vernesong/OpenClash/core/master/premium/clash-{clash_arch}-{tun_v}.gz",
                                os.path.join(tmpdir.name, "clash_tun.gz")))
        dl_tasks.append(dl2(f"https://raw.githubusercontent.com/vernesong/OpenClash/core/master/meta/clash-{clash_arch}.tar.gz",
                                os.path.join(tmpdir.name, "clash_meta.tar.gz")))
        dl_tasks.append(dl2(f"https://raw.githubusercontent.com/vernesong/OpenClash/core/master/dev/clash-{clash_arch}.tar.gz",
                                os.path.join(tmpdir.name, "clash.tar.gz")))

    wait_dl_tasks(dl_tasks)
    # 解压
    if os.path.isfile(os.path.join(tmpdir.name, "AdGuardHome.tar.gz")):
        with tarfile.open(os.path.join(tmpdir.name, "AdGuardHome.tar.gz"), "r:gz") as tar:
            if file := tar.extractfile("./AdGuardHome/AdGuardHome"):
                with open(os.path.join(files_path, "usr", "bin", "AdGuardHome", "AdGuardHome"), "wb") as f:
                    f.write(file.read())
                os.chmod(os.path.join(files_path, "usr", "bin", "AdGuardHome", "AdGuardHome"), 0o755)  # noqa: S103

    clash_core_path = os.path.join(files_path, "etc", "openclash", "core")
    if not os.path.isdir(clash_core_path):
        os.makedirs(clash_core_path)
    if os.path.isfile(os.path.join(tmpdir.name, "clash_tun.gz")):
        with gzip.open(os.path.join(tmpdir.name, "clash_tun.gz"), 'rb') as f_in, open(os.path.join(clash_core_path, "clash_tun"), 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.chmod(os.path.join(clash_core_path, "clash_tun"), 0o755)  # noqa: S103

    if os.path.isfile(os.path.join(tmpdir.name, "clash_meta.tar.gz")):
        with tarfile.open(os.path.join(tmpdir.name, "clash_meta.tar.gz"), "r:gz") as tar:
            if file := tar.extractfile("clash"):
                with open(os.path.join(clash_core_path, "clash_meta"), "wb") as f:
                    f.write(file.read())
                os.chmod(os.path.join(clash_core_path, "clash_meta"), 0o755)  # noqa: S103

    if os.path.isfile(os.path.join(tmpdir.name, "clash.tar.gz")):
        with tarfile.open(os.path.join(tmpdir.name, "clash.tar.gz"), "r:gz") as tar:
            if file := tar.extractfile("clash"):
                with open(os.path.join(clash_core_path, "clash"), "wb") as f:
                    f.write(file.read())
                os.chmod(os.path.join(clash_core_path, "clash"), 0o755)  # noqa: S103

    tmpdir.cleanup()

    # 获取bt_trackers
    bt_tracker = request_get("https://github.com/XIU2/TrackersListCollection/raw/master/all_aria2.txt")
    # 替换信息
    with open(os.path.join(files_path, "etc", "uci-defaults", "zzz-chenmozhijin"), encoding="utf-8") as f:
        content = f.read()
    with open(os.path.join(files_path, "etc", "uci-defaults", "zzz-chenmozhijin"), "w", encoding="utf-8") as f:
        for line in content.splitlines():
            if line.startswith("  uci set aria2.main.bt_tracker="):
                f.write(f"  uci set aria2.main.bt_tracker='{bt_tracker}'\n")
            elif line.startswith("uci set network.lan.ipaddr="):
                f.write(f"uci set network.lan.ipaddr='{config["openwrtext"]["ipaddr"]}'\n")
            elif "Compiled by 沉默の金" in line:
                f.write(line.replace("Compiled by 沉默の金", f"Compiled by {compiler}") + "\n")
            else:
                f.write(line + "\n")

    logger.info("其他处理")
    with open(os.path.join(openwrt.path, "package", "base-files", "files", "bin", "config_generate"), encoding="utf-8") as f:
        content = f.read()
    with open(os.path.join(openwrt.path, "package", "base-files", "files", "bin", "config_generate"), "w", encoding="utf-8") as f:
        for line in content.splitlines():
            if "set system.@system[-1].hostname='OpenWrt'" in line:
                f.write(line.replace("set system.@system[-1].hostname='OpenWrt'", "set system.@system[-1].hostname='OpenWrt-k'") + "\n")
            elif "set system.@system[-1].timezone='UTC'" in line:
                f.write(line.replace("set system.@system.@system[-1].timezone='UTC'",
                                     f"set system.@system[-1].timezone='{config['openwrtext']['timezone']}'") +
                                     f"\n		set system.@system[-1].zonename='{config["openwrtext"]["zonename"]}'\n")
            else:
                f.write(line + "\n")

    logger.info("%s生成源代码归档")
    os.makedirs(os.path.join(paths.uploads, cfg_name), exist_ok=True)
    tar_path = os.path.join(paths.uploads, cfg_name, "openwrt-source.tar.xz")
    with tarfile.open(tar_path, "w:xz") as tar:
        tar.add(openwrt.path, arcname="openwrt")
    return cfg_name, config, tar_path