# -*- coding: utf-8 -*-
"""
微光-Daily Care - 常用城市中英文对照表
用于 Open-Meteo Geocoding 无法正确解析中文城市名时的兜底转换
"""
# 主要城市 + 省会 + 常见地级市
CITY_MAP = {
    "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou", "深圳": "Shenzhen",
    "长沙": "Changsha", "武汉": "Wuhan", "成都": "Chengdu", "重庆": "Chongqing",
    "杭州": "Hangzhou", "南京": "Nanjing", "天津": "Tianjin", "西安": "Xi'an",
    "郑州": "Zhengzhou", "济南": "Jinan", "青岛": "Qingdao", "沈阳": "Shenyang",
    "大连": "Dalian", "长春": "Changchun", "哈尔滨": "Harbin", "石家庄": "Shijiazhuang",
    "太原": "Taiyuan", "呼和浩特": "Hohhot", "合肥": "Hefei", "福州": "Fuzhou",
    "厦门": "Xiamen", "南昌": "Nanchang", "南宁": "Nanning", "海口": "Haikou",
    "贵阳": "Guiyang", "昆明": "Kunming", "拉萨": "Lhasa", "兰州": "Lanzhou",
    "西宁": "Xining", "银川": "Yinchuan", "乌鲁木齐": "Urumqi", "苏州": "Suzhou",
    "无锡": "Wuxi", "宁波": "Ningbo", "温州": "Wenzhou", "佛山": "Foshan",
    "东莞": "Dongguan", "珠海": "Zhuhai", "惠州": "Huizhou", "中山": "Zhongshan",
    "徐州": "Xuzhou", "常州": "Changzhou", "南通": "Nantong", "烟台": "Yantai",
    "潍坊": "Weifang", "临沂": "Linyi", "洛阳": "Luoyang", "唐山": "Tangshan",
    "保定": "Baoding", "邯郸": "Handan", "吉林": "Jilin", "宜昌": "Yichang",
    "襄阳": "Xiangyang", "岳阳": "Yueyang", "株洲": "Zhuzhou", "湘潭": "Xiangtan",
    "衡阳": "Hengyang", "常德": "Changde", "赣州": "Ganzhou", "九江": "Jiujiang",
    "汕头": "Shantou", "湛江": "Zhanjiang", "桂林": "Guilin", "柳州": "Liuzhou",
    "遵义": "Zunyi", "绵阳": "Mianyang", "德阳": "Deyang", "咸阳": "Xianyang",
    "宝鸡": "Baoji", "大同": "Datong", "秦皇岛": "Qinhuangdao", "张家口": "Zhangjiakou",
    "三亚": "Sanya", "绍兴": "Shaoxing", "嘉兴": "Jiaxing", "金华": "Jinhua",
    "台州": "Taizhou", "泉州": "Quanzhou", "漳州": "Zhangzhou", "莆田": "Putian",
    "扬州": "Yangzhou", "镇江": "Zhenjiang", "盐城": "Yancheng", "淮安": "Huaian",
    "芜湖": "Wuhu", "蚌埠": "Bengbu", "安庆": "Anqing", "马鞍山": "Ma'anshan",
    "洛阳": "Luoyang", "开封": "Kaifeng", "新乡": "Xinxiang", "南阳": "Nanyang",
    "廊坊": "Langfang", "沧州": "Cangzhou", "邢台": "Xingtai", "包头": "Baotou",
    "鄂尔多斯": "Ordos", "赤峰": "Chifeng", "威海": "Weihai", "淄博": "Zibo",
    "日照": "Rizhao", "泰安": "Taian", "济宁": "Jining", "菏泽": "Heze",
    "聊城": "Liaocheng", "德州": "Dezhou", "滨州": "Binzhou", "东营": "Dongying",
    "徐州": "Xuzhou", "连云港": "Lianyungang", "宿迁": "Suqian", "盐城": "Yancheng",
    "绵阳": "Mianyang", "南充": "Nanchong", "宜宾": "Yibin", "泸州": "Luzhou",
    "攀枝花": "Panzhihua", "乐山": "Leshan", "眉山": "Meishan", "广安": "Guangan",
    "达州": "Dazhou", "雅安": "Yaan", "遂宁": "Suining", "内江": "Neijiang",
    "资阳": "Ziyang", "自贡": "Zigong", "六盘水": "Liupanshui", "毕节": "Bijie",
    "铜仁": "Tongren", "安顺": "Anshun", "曲靖": "Qujing", "玉溪": "Yuxi",
    "大理": "Dali", "丽江": "Lijiang", "香格里拉": "Shangri-La", "西双版纳": "Xishuangbanna",
    "恩施": "Enshi", "十堰": "Shiyan", "荆州": "Jingzhou", "黄冈": "Huanggang",
    "孝感": "Xiaogan", "黄石": "Huangshi", "咸宁": "Xianning", "鄂州": "Ezhou",
    "荆门": "Jingmen", "随州": "Suizhou", "张家界": "Zhangjiajie", "怀化": "Huaihua",
    "郴州": "Chenzhou", "永州": "Yongzhou", "邵阳": "Shaoyang", "益阳": "Yiyang",
    "娄底": "Loudi", "湘西": "Xiangxi", "赣州": "Ganzhou", "宜春": "Yichun",
    "上饶": "Shangrao", "抚州": "Fuzhou", "景德镇": "Jingdezhen", "萍乡": "Pingxiang",
    "新余": "Xinyu", "鹰潭": "Yingtan", "吉安": "Jian", "清远": "Qingyuan",
    "韶关": "Shaoguan", "梅州": "Meizhou", "河源": "Heyuan", "阳江": "Yangjiang",
    "茂名": "Maoming", "肇庆": "Zhaoqing", "江门": "Jiangmen", "云浮": "Yunfu",
    "揭阳": "Jieyang", "潮州": "Chaozhou", "汕尾": "Shanwei", "梧州": "Wuzhou",
    "北海": "Beihai", "防城港": "Fangchenggang", "钦州": "Qinzhou", "贵港": "Guigang",
    "玉林": "Yulin", "百色": "Baise", "贺州": "Hezhou", "河池": "Hechi",
    "来宾": "Laibin", "崇左": "Chongzuo", "儋州": "Danzhou", "琼海": "Qionghai",
    "义乌": "Yiwu", "昆山": "Kunshan", "江阴": "Jiangyin", "常熟": "Changshu",
    "张家港": "Zhangjiagang", "太仓": "Taicang", "慈溪": "Cixi", "余姚": "Yuyao",
    "诸暨": "Zhuji", "海宁": "Haining", "桐乡": "Tongxiang", "温岭": "Wenling",
    "乐清": "Yueqing", "瑞安": "Ruian", "晋江": "Jinjiang", "石狮": "Shishi",
    "南安": "Nan'an", "龙海": "Longhai", "龙岩": "Longyan", "南平": "Nanping",
    "宁德": "Ningde", "三明": "Sanming", "滁州": "Chuzhou", "阜阳": "Fuyang",
    "宿州": "Suzhou", "亳州": "Bozhou", "池州": "Chizhou", "宣城": "Xuancheng",
    "淮南": "Huainan", "淮北": "Huaibei", "铜陵": "Tongling", "黄山": "Huangshan",
    "香港": "Hong Kong", "澳门": "Macau", "台北": "Taipei", "高雄": "Kaohsiung",
}


def to_english(city: str) -> str:
    """中文城市名 → 英文名；已含英文直接返回"""
    city = (city or "").strip()
    if not city:
        return ""
    if city in CITY_MAP:
        return CITY_MAP[city]
    # 已含 ASCII 直接返回（如 "Changsha"）
    if all(ord(c) < 128 or c in "'- " for c in city):
        return city
    return city


# Open-Meteo 解析不准的少数城市直接内置坐标（纬度, 经度）
COORD_FIX = {
    "西安": (34.3416, 108.9398),
    "Xi'an": (34.3416, 108.9398),
    "Xian": (34.3416, 108.9398),
    "三亚": (18.2528, 109.5119),
    "丽江": (26.8721, 100.2299),
    "大理": (25.6065, 100.2676),
    "香格里拉": (27.8255, 99.7065),
    "拉萨": (29.6520, 91.1721),
    # 拼音撞车城市（Open-Meteo 常解析到同名城市）
    "苏州": (31.2989, 120.5853),
    "Suzhou": (31.2989, 120.5853),
    "泉州": (24.8741, 118.6757),
    "Quanzhou": (24.8741, 118.6757),
    "吉安": (27.1138, 114.9929),
    "Jian": (27.1138, 114.9929),
    "榆林": (38.2853, 109.7346),
    "Yulin": (38.2853, 109.7346),
    "阜阳": (32.8901, 115.8142),
    "Fuyang": (32.8901, 115.8142),
    # countryCode=CN 过滤会误伤的地区
    "香港": (22.3193, 114.1694),
    "Hong Kong": (22.3193, 114.1694),
    "澳门": (22.1987, 113.5439),
    "Macau": (22.1987, 113.5439),
    "台北": (25.0330, 121.5654),
    "Taipei": (25.0330, 121.5654),
    "高雄": (22.6273, 120.3014),
    "Kaohsiung": (22.6273, 120.3014),
}
