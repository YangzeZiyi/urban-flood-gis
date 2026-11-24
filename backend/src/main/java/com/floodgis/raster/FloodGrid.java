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

        // ================================================================
        // TODO ★ 把"经纬度"换算成"第几行、第几列"，两行 ★
        //
        //   格子是均匀铺的：
        //     横向：从西边界 west 到东边界 east，分成 width 列
        //     纵向：从北边界 north 到南边界 south，分成 height 行
        //
        //   所以：
        //     列号 = (经度 - west) / (east - west) * width
        //     行号 = (north - 纬度) / (north - south) * height
        //
        //   ⚠️ 行号这里是 (north - 纬度)，不是 (纬度 - south)。
        //      图像的第 0 行在最北边，越往南行号越大。
        //      写成 (纬度 - south) 会让图上南边的点读到北边的值——
        //      不报错，值也"像那么回事"，是最难查的一类 bug。
        //
        //   取完整数后还要"夹"一下：经度正好等于东边界时，
        //   (lon-west)/(east-west)*width 会等于 width，越界一格。
        //
        //   下面两行是**故意写错的**（永远读左上角第一格），改掉它们。
        // ================================================================
        int col = 0;          // <- 改这一行
        int row = 0;          // <- 改这一行

        if (col < 0 || col >= width || row < 0 || row >= height) {
            return null;
        }

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
}
