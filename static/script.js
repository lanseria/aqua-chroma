// static/script.js
// 仪表盘前端：适配 GET /api/results 的 R_success 响应结构，
// 展示海蓝程度 / 云层覆盖率 / 能见度三项指标。

document.addEventListener('DOMContentLoaded', () => {
    // --- DOM 元素获取 ---
    const container = document.getElementById('results-container');
    const statusDiv = document.getElementById('status');
    const loader = document.getElementById('loader');
    const chartDom = document.getElementById('echarts-container');

    // --- 状态管理 ---
    let allData = [];
    let currentIndex = 0;
    const itemsPerPage = 12;
    let isLoading = false;
    let myChart = echarts.init(chartDom, 'dark'); // 初始化 ECharts 实例

    // --- 辅助函数：根据数值获取颜色 ---
    const getBluenessColor = (p) => {
        if (p < 20) return '#8c6b4f'; // 浑浊的棕色
        if (p < 50) return '#6495ED'; // 矢车菊蓝
        if (p < 80) return '#00BFFF'; // 深天蓝
        return '#1E90FF';   // 道奇蓝
    };

    const getCloudColor = (p) => {
        if (p < 20) return '#ADD8E6'; // 浅蓝色 (晴朗)
        if (p < 50) return '#D3D3D3'; // 浅灰色 (少云)
        if (p < 80) return '#A9A9A9'; // 深灰色 (多云)
        return '#696969';   // 暗灰色 (阴天)
    };

    const getVisibilityColor = (km) => {
        if (km == null) return '#555555';
        if (km <= 0.5) return '#4a4a4a'; // 极端浑浊/浓雾（接近 0）
        if (km < 4) return '#8B0000';    // 浓雾 (暗红)
        if (km < 10) return '#DAA520';   // 轻度霾/雾 (goldenrod)
        if (km < 20) return '#87CEEB';   // 中等 (天空蓝)
        return '#1E90FF';                // 良好 (道奇蓝)
    };

    // --- ECharts 渲染函数 ---
    const renderChart = (data) => {
        const bluenessData = [];
        const cloudData = [];
        const visibilityData = [];
        data.forEach(item => {
            const ts = new Date(item.timestamp * 1000);
            if (item.sea_blueness != null) bluenessData.push([ts, +(item.sea_blueness * 100).toFixed(2)]);
            if (item.cloud_coverage != null) cloudData.push([ts, +(item.cloud_coverage * 100).toFixed(2)]);
            if (item.visibility_km != null) visibilityData.push([ts, item.visibility_km]);
        });

        const option = {
            backgroundColor: 'transparent',
            tooltip: { trigger: 'axis' },
            legend: { data: ['海蓝程度', '云层覆盖率', '能见度'] },
            grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
            xAxis: { type: 'time', boundaryGap: false },
            yAxis: [
                { type: 'value', min: 0, max: 100, name: '%', axisLabel: { formatter: '{value} %' } },
                { type: 'value', min: 0, max: 35, name: 'km', axisLabel: { formatter: '{value} km' } }
            ],
            series: [
                {
                    name: '海蓝程度',
                    type: 'line',
                    smooth: true,
                    symbol: 'none',
                    data: bluenessData,
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                            offset: 0, color: 'rgba(30, 144, 255, 0.5)'
                        }, {
                            offset: 1, color: 'rgba(30, 144, 255, 0)'
                        }])
                    }
                },
                {
                    name: '云层覆盖率',
                    type: 'line',
                    smooth: true,
                    symbol: 'none',
                    data: cloudData,
                    lineStyle: { color: '#D3D3D3' },
                    itemStyle: { color: '#D3D3D3' }
                },
                {
                    name: '能见度',
                    type: 'line',
                    smooth: true,
                    symbol: 'none',
                    yAxisIndex: 1,
                    data: visibilityData,
                    lineStyle: { color: '#20B2AA' },
                    itemStyle: { color: '#20B2AA' }
                }
            ]
        };
        myChart.setOption(option);
    };

    // --- 能见度展示片段（进度条按 km/35km 比例填充；云遮蔽记 0）---
    const visibilityHtml = (visibilityKm, visibilityLevel) => {
        if (visibilityKm == null) return ''; // 旧记录/无数据时不展示该行
        const pct = Math.min(100, (visibilityKm / 35.0) * 100);
        const label = visibilityLevel ? ` (${visibilityLevel})` : '';
        return `
                    <!-- 能见度进度条 -->
                    <div class="progress-container">
                        <div class="progress-label">
                            <span>能见度${label}</span>
                            <span>${visibilityKm.toFixed(1)} km</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-bar-fill" data-width="${pct}%" style="background-color: ${getVisibilityColor(visibilityKm)};"></div>
                        </div>
                    </div>`;
    };

    // --- 卡片渲染函数 ---
    const renderItems = () => {
        if (currentIndex >= allData.length) {
            loader.classList.add('hidden');
            return;
        }
        const itemsToRender = allData.slice(currentIndex, currentIndex + itemsPerPage);

        itemsToRender.forEach(item => {
            const card = document.createElement('div');
            card.className = 'result-card';
            const formattedDate = new Date(item.timestamp * 1000).toLocaleString('zh-CN', { hour12: false });

            const bluenessPercent = (item.sea_blueness ?? 0) * 100;
            const cloudPercent = (item.cloud_coverage ?? 0) * 100;

            card.innerHTML = `
                <div class="card-header"><h2>${formattedDate}</h2></div>
                <div class="card-body">
                    <p>状态: <span class="value">${item.status}</span></p>

                    <!-- 海蓝程度进度条 -->
                    <div class="progress-container">
                        <div class="progress-label">
                            <span>海蓝程度</span>
                            <span>${bluenessPercent.toFixed(2)}%</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-bar-fill" data-width="${bluenessPercent}%" style="background-color: ${getBluenessColor(bluenessPercent)};"></div>
                        </div>
                    </div>

                    <!-- 云层覆盖率进度条 -->
                    <div class="progress-container">
                        <div class="progress-label">
                            <span>云层覆盖率</span>
                            <span>${cloudPercent.toFixed(2)}%</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-bar-fill" data-width="${cloudPercent}%" style="background-color: ${getCloudColor(cloudPercent)};"></div>
                        </div>
                    </div>
${visibilityHtml(item.visibility_km, item.visibility_level)}
                </div>
                <div class="image-gallery">
                    <figure>
                        <img src="/${item.output_directory}/03_ocean_only.png" alt="海洋区域" loading="lazy">
                        <figcaption>海洋区域</figcaption>
                    </figure>
                    <figure>
                        <img src="/${item.output_directory}/04_hsv_classification.png" alt="颜色分类" loading="lazy">
                        <figcaption>颜色分类</figcaption>
                    </figure>
                </div>
            `;
            container.appendChild(card);
        });

        // 延迟一小段时间再设置宽度，以触发CSS动画
        setTimeout(() => {
            const newFills = container.querySelectorAll('.progress-bar-fill:not(.animated)');
            newFills.forEach(fill => {
                fill.style.width = fill.getAttribute('data-width');
                fill.classList.add('animated');
            });
        }, 100);

        currentIndex += itemsPerPage;
    };

    // --- 滚动和初始化逻辑 ---
    const handleScroll = () => {
        if (isLoading || currentIndex >= allData.length) return;
        if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 300) {
            isLoading = true;
            loader.classList.remove('hidden');
            setTimeout(() => {
                renderItems();
                isLoading = false;
            }, 500);
        }
    };

    const init = async () => {
        try {
            const response = await fetch('/api/results');
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

            const payload = await response.json();
            if (payload.code !== 200 || !Array.isArray(payload.data)) {
                throw new Error(payload.msg || '数据格式不正确');
            }
            const data = payload.data;
            if (data.length === 0) {
                statusDiv.textContent = '暂无分析数据，请等待后台任务执行...';
                return;
            }

            // 【重要】先用原始顺序数据渲染图表
            renderChart(data);

            // 然后再倒序数据用于卡片展示
            allData = data.slice().reverse();
            statusDiv.textContent = `共加载 ${allData.length} 条记录。向下滚动以查看更多。`;

            renderItems();
            window.addEventListener('scroll', handleScroll);

        } catch (error) {
            console.error("获取数据失败:", error);
            statusDiv.textContent = '获取数据失败，请检查后端服务是否正常。';
        }
    };

    // 监听窗口大小变化，使图表自适应
    window.addEventListener('resize', () => {
        myChart.resize();
    });

    // 启动应用
    init();
});
