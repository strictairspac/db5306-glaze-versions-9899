export const topic = {
  "name": "瓷汀工场·釉料版次留存",
  "direction": "MVCC 可见性与垃圾回收",
  "modules": [
    "配方版本",
    "读快照",
    "回收预检",
    "留存批单"
  ],
  "algorithm": "以活跃快照低水位计算可见版本和可安全清理的历史版本"
};
