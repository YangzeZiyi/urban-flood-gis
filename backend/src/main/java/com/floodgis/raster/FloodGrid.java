package com.floodgis.raster;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;

/**
 * 易涝指数网格的随机访问。
 *
 * <h3>它读的是什么</h3>
 * <p>{@code pipeline/process/flood_index.py} 会同时产出两个文件：</p>
 * <pre>
 *   wuhan_flood_index_grid.json   头文件：宽高、经纬度范围、通道顺序、分级阈值
 *   wuhan_flood_index_grid.bin    数据：float32 平铺，行优先，每格 4 个通道
 *                                 [index, elev, slope, acc]
 * </pre>
 *
 * <h3>为什么用二进制而不是数据库</h3>
 * <p>277 万个格子。存 PostGIS 要上百万行几何、几个 GB；存成二进制是 44 MB，
 * 而且"按坐标取一格"退化成一次乘法和一次 seek——O(1)，不用索引、不用连接池。
 * 这是栅格数据最自然的存法，GDAL、GeoTIFF 内部也是这么干的。</p>
 *
 * <h3>三个容易踩的坑</h3>
 * <ol>
 *   <li><b>字节序</b>：numpy 默认写小端（little-endian），Java 的
 *       {@code DataInputStream.readFloat()} 读的是大端。直接读会得到
 *       一堆 {@code 1.0E-38} 这种垃圾数，而且不报错。必须显式指定 LITTLE_ENDIAN。</li>
 *   <li><b>行的方向</b>：图像的第 0 行在最<b>北</b>边，纬度越大行号越小。
 *       写成 {@code (lat - south)} 就上下颠倒，画面南边的点会读到北边的数据，
 *       而且值看起来"也像那么回事"，极难发现。</li>
 *   <li><b>列号越界</b>：经度正好等于东边界时，{@code (lon-west)/dlon} 会算出
 *       width，取出来是下一行的第一个格子。必须夹到 [0, width-1]。</li>
 * </ol>
 */
public class FloodGrid {

    /** 每格字节数：4 个 float32。这个数字变了，Python 那边的 layout 也要改。 */
    public static final int BYTES_PER_CELL = 16;

    private final int width;
    private final int height;
    private final double west;
    private final double south;
    private final double east;
    private final double north;
    private final List<Level> levels;
    private final FileChannel channel;
    private final Path binPath;

    private FloodGrid(int width, int height, double west, double south,
                      double east, double north, List<Level> levels,
                      FileChannel channel, Path binPath) {
        this.width = width;
        this.height = height;
        this.west = west;
        this.south = south;
        this.east = east;
        this.north = north;
        this.levels = levels;
        this.channel = channel;
        this.binPath = binPath;
    }

    /**
     * 从 JSON 头文件加载。.bin 不读进内存，只打开通道，用的时候按偏移量读。
     */
    public static FloodGrid load(Path jsonPath, ObjectMapper mapper) throws IOException {
        JsonNode root = mapper.readTree(Files.readString(jsonPath));

        int width = root.get("width").asInt();
        int height = root.get("height").asInt();
        JsonNode bbox = root.get("bbox_wgs84");
        double west = bbox.get(0).asDouble();
        double south = bbox.get(1).asDouble();
        double east = bbox.get(2).asDouble();
        double north = bbox.get(3).asDouble();

        List<Level> levels = new ArrayList<>();
        for (JsonNode lv : root.get("levels")) {
            levels.add(new Level(
                    lv.get("name").asText(),
                    lv.get("lo").asDouble(),
                    lv.get("hi").asDouble(),
                    lv.get("color").asText()));
        }

        Path binPath = jsonPath.resolveSibling(root.get("file").asText());
        if (!Files.exists(binPath)) {
            throw new IOException("头文件指向的数据文件不存在：" + binPath);
        }
        long expected = (long) width * height * BYTES_PER_CELL;
        long actual = Files.size(binPath);
        if (actual != expected) {
            throw new IOException("数据文件大小不对：期望 " + expected + " 字节，实际 " + actual
                    + "。八成是头文件和数据文件不是同一次生成的，重跑 flood_index.py");
        }

        FileChannel channel = FileChannel.open(binPath, StandardOpenOption.READ);
        return new FloodGrid(width, height, west, south, east, north, levels, channel, binPath);
    }

    /**
     * 按经纬度取一格。
     *
     * @return 越界返回 null；格子在范围内但 index 是 NaN（水体、市域外）返回的 Cell.evaluated() 为 false
     */
    public Cell read(double lon, double lat) throws IOException {
        if (lon < west || lon > east || lat < south || lat > north) {
            return null;
        }

        // 经纬度 → 行列号。你写的那两行现在住在下面的 rowOf() / colOf() 里。
        // 为什么抽出去：框选统计要用同一套换算。复制一份的话，
        // 将来有人改了取整方式只改一处，单点查询和统计就会差一格——
        // 而且两边看起来都"正常"，没人会发现。同一件事只写一次。
        int col = colOf(lon);
        int row = rowOf(lat);

        long offset = ((long) row * width + col) * BYTES_PER_CELL;

        // 定位读取：把 16 字节读到这个小缓冲区里。
        // FileChannel.read(buf, position) 是"定位读"，不移动文件指针，
        // 所以多线程同时点也不会互相踩（RandomAccessFile.seek 就会）。
        ByteBuffer buf = ByteBuffer.allocate(BYTES_PER_CELL).order(ByteOrder.LITTLE_ENDIAN);
        int n = channel.read(buf, offset);
        if (n != BYTES_PER_CELL) {
            throw new IOException("读取 " + binPath.getFileName() + " 偏移 " + offset
                    + " 时只读到 " + n + " 字节");
        }
        buf.flip();
        float index = buf.getFloat();
        float elev = buf.getFloat();
        float slope = buf.getFloat();
        float acc = buf.getFloat();

        return new Cell(row, col, index, elev, slope, acc);
    }

    /**
     * 经度 → 列号，夹在 [0, width-1]。
     *
     * <p>夹的意义：经度正好等于东边界时，{@code (lon-west)/(east-west)*width}
     * 恰好等于 width，取出来是"下一行的第一个格子"——不报错，但是错的。</p>
     */
    public int colOf(double lon) {
        int c = (int) Math.floor((lon - west) / (east - west) * width);
        return Math.max(0, Math.min(width - 1, c));
    }

    /** 纬度 → 行号，夹在 [0, height-1]。注意分子是 {@code north - lat}：第 0 行在最北边。 */
    public int rowOf(double lat) {
        int r = (int) Math.floor((north - lat) / (north - south) * height);
        return Math.max(0, Math.min(height - 1, r));
    }

    /** 列号 → 格子中心的经度。统计结果要反查"最高点在哪"时用。 */
    public double lonOf(int col) {
        return west + (col + 0.5) * (east - west) / width;
    }

    /** 行号 → 格子中心的纬度。 */
    public double latOf(int row) {
        return north - (row + 0.5) * (north - south) / height;
    }

    /**
     * 框选统计：把一块经纬度矩形内的所有格子扫一遍。
     *
     * <h3>为什么要分块读</h3>
     * <p>整幅 44 MB，全读进内存也能跑，但那是"每个请求 44 MB"，几个人同时框选就爆了。
     * 这里每次只读 128 行（约 3.4 MB），读完即弃。</p>
     *
     * <h3>为什么面积要逐行算，不能用一个常数</h3>
     * <p>网格是按<b>经纬度</b>等分的，不是按米。1 度经度在赤道是 111.3 km，
     * 在 30°N 只有 96.4 km——同一个格子在北边和南边面积差 13%。
     * 用平均值凑一个常数，全市面积的误差能到几十平方公里。
     * 所以每一行按该行的纬度算一次格面积，再乘这一行的格子数。</p>
     *
     * @return 与网格有交集时返回统计结果；完全在网格外返回 null
     */
    public Summary summarize(double w, double s, double e, double n) throws IOException {
        // 先把框裁到网格范围内——用户可以把矩形拖到省外
        double ww = Math.max(w, west);
        double ee = Math.min(e, east);
        double ss = Math.max(s, south);
        double nn = Math.min(n, north);
        if (ww >= ee || ss >= nn) {
            return null;
        }

        int c0 = colOf(ww);
        int c1 = colOf(ee);
        int r0 = rowOf(nn);          // 北边界 → 小行号
        int r1 = rowOf(ss);          // 南边界 → 大行号
        int colCount = c1 - c0 + 1;
        int rowCount = r1 - r0 + 1;

        long cells = (long) colCount * rowCount;
        long evaluated = 0;
        long noData = 0;
        double sum = 0;
        double areaKm2 = 0;
        double max = Double.NEGATIVE_INFINITY;
        int maxRow = -1;
        int maxCol = -1;

        double[] levelArea = new double[levels.size()];

        // 中位数和 p90 需要把所有有效值排一遍。只留有效值，水体不占空间。
        float[] vals = new float[(int) Math.min(cells, 1 << 20)];
        int nVal = 0;

        double dLon = (east - west) / width;
        double dLat = (north - south) / height;
        final double KM_PER_DEG_LAT = 110.574;
        final double KM_PER_DEG_LON_EQUATOR = 111.320;

        final int CHUNK_ROWS = 128;
        int chunkRows = Math.min(CHUNK_ROWS, rowCount);
        ByteBuffer buf = ByteBuffer.allocate(chunkRows * width * BYTES_PER_CELL)
                .order(ByteOrder.LITTLE_ENDIAN);

        for (int rs = r0; rs <= r1; rs += chunkRows) {
            int rows = Math.min(chunkRows, r1 - rs + 1);
            int want = rows * width * BYTES_PER_CELL;
            buf.clear();
            long offset = (long) rs * width * BYTES_PER_CELL;
            while (buf.position() < want) {
                int got = channel.read(buf, offset + buf.position());
                if (got <= 0) {
                    throw new IOException("读取 " + binPath.getFileName()
                            + " 在第 " + (offset + buf.position()) + " 字节提前结束");
                }
            }
            buf.flip();

            for (int r = 0; r < rows; r++) {
                int row = rs + r;
                double latRow = latOf(row);
                double cellKm2 = (dLat * KM_PER_DEG_LAT)
                        * (dLon * KM_PER_DEG_LON_EQUATOR * Math.cos(Math.toRadians(latRow)));
                int rowBase = r * width;

                for (int c = c0; c <= c1; c++) {
                    float v = buf.getFloat((rowBase + c) * BYTES_PER_CELL);

                    // ============================================================
                    // TODO ★ 五行：决定"哪些格子算进统计" ★
                    //
                    //  下面这段是**故意写错的**——少了"跳过 NaN"这一步。
                    //  跑一次会看到三个症状：
                    //    1. 平均指数显示成「—」（一个 NaN 就把整个 sum 污染成 NaN）
                    //    2. "极高"一档面积暴涨（NaN 和任何数比较都是 false，
                    //       levelIndex 找不到区间，就落到最后一档）
                    //    3. 页面底部"水体与市域外 X 格不计"永远是 0
                    //
                    //  要补的三件事：
                    //    (1) index 是 NaN 的格子（水体、市域外）整格跳过：
                    //          if (Float.isNaN(v)) { noData++; continue; }
                    //        为什么不能顺手加 0：
                    //          水体越大，均值被拉得越低。那不是"保守"，
                    //          是把"没有数据"当成了"很安全"——两个完全不同的结论。
                    //    (2) 其余格子：evaluated++;  sum += v;  areaKm2 += cellKm2;
                    //    (3) 按等级累加面积：levelArea[levelIndex(v)] += cellKm2;
                    // ============================================================
                    evaluated++;
                    sum += v;
                    areaKm2 += cellKm2;
                    levelArea[levelIndex(v)] += cellKm2;

                    if (nVal == vals.length) {
                        vals = java.util.Arrays.copyOf(vals, vals.length * 2);
                    }
                    vals[nVal++] = v;

                    if (v > max) {
                        max = v;
                        maxRow = row;
                        maxCol = c;
                    }
                }
            }
        }

        float[] sorted = java.util.Arrays.copyOf(vals, nVal);
        java.util.Arrays.sort(sorted);
        double median = percentile(sorted, 0.50);
        double p90 = percentile(sorted, 0.90);

        List<LevelStat> levelStats = new ArrayList<>(levels.size());
        for (int i = 0; i < levels.size(); i++) {
            Level lv = levels.get(i);
            double km2 = levelArea[i];
            double pct = areaKm2 > 0 ? km2 / areaKm2 * 100 : 0;
            levelStats.add(new LevelStat(lv.name(), lv.color(), km2, pct));
        }

        double bboxKm2 = bboxAreaKm2(ww, ss, ee, nn);
        double mean = evaluated > 0 ? sum / evaluated : Double.NaN;

        return new Summary(
                ww, ss, ee, nn,
                cells, evaluated, noData,
                round3(areaKm2), round3(bboxKm2),
                round3(mean), round3(median), round3(p90),
                evaluated > 0 ? round3(max) : Double.NaN,
                maxRow >= 0 ? lonOf(maxCol) : Double.NaN,
                maxRow >= 0 ? latOf(maxRow) : Double.NaN,
                levelStats);
    }

    /** 矩形本身的面积（球面近似），用来和"参与评价面积"对比。 */
    private static double bboxAreaKm2(double w, double s, double e, double n) {
        double midLat = Math.toRadians((s + n) / 2);
        double widthKm = (e - w) * 111.320 * Math.cos(midLat);
        double heightKm = (n - s) * 110.574;
        return widthKm * heightKm;
    }

    /** 已排序数组的分位数。空数组返回 NaN。 */
    private static double percentile(float[] sorted, double q) {
        if (sorted.length == 0) {
            return Double.NaN;
        }
        int i = (int) Math.round(q * (sorted.length - 1));
        return sorted[Math.max(0, Math.min(sorted.length - 1, i))];
    }

    /** 指数落在第几档。阈值来自 JSON，这里不写死。 */
    private int levelIndex(float index) {
        for (int i = 0; i < levels.size(); i++) {
            Level lv = levels.get(i);
            if (index >= lv.lo() && index < lv.hi()) {
                return i;
            }
        }
        return levels.size() - 1;
    }

    private static double round3(double v) {
        if (Double.isNaN(v)) {
            return v;
        }
        return Math.round(v * 1000) / 1000.0;
    }

    /** 判定等级。阈值来自 JSON，不在这里硬编码。 */
    public Level levelOf(float index) {
        for (Level lv : levels) {
            if (index >= lv.lo() && index < lv.hi()) {
                return lv;
            }
        }
        return levels.get(levels.size() - 1);
    }

    public int width() {
        return width;
    }

    public int height() {
        return height;
    }

    public List<Level> levels() {
        return levels;
    }

    public Path binPath() {
        return binPath;
    }

    /** 一格的全部通道。index 为 NaN 表示"这里不参与评价"（水体或市域外）。 */
    public record Cell(int row, int col, float index, float elev, float slope, float acc) {
        public boolean evaluated() {
            return !Float.isNaN(index);
        }
    }

    /** 一个风险等级：名字、下限、上限、配色。 */
    public record Level(String name, double lo, double hi, String color) {
    }

    /** 框选后的单档统计：名字、配色、面积、占比（分母是参与评价面积）。 */
    public record LevelStat(String name, String color, double km2, double pct) {
    }

    /**
     * 框选统计的完整结果。
     *
     * <p>{@code cells} 是框内的总格子数，{@code evaluated} 是真正参与评价的
     * （去掉水体和市域外），两者之差就是 {@code noData}。
     * 面积给两个：{@code bboxKm2} 是框的几何面积，{@code areaKm2} 是参与评价的面积——
     * 框住半个东湖时，这两个数字会差很多，正好用来说明"水面不算陆地"。</p>
     */
    public record Summary(
            double west, double south, double east, double north,
            long cells, long evaluated, long noData,
            double areaKm2, double bboxKm2,
            double mean, double median, double p90, double max,
            double maxLon, double maxLat,
            List<LevelStat> levels) {
    }
}
